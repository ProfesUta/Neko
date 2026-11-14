"""Local, calibrated pixel evidence. No model, memory reading or route inference."""
import math
import time
from collections import deque
from dataclasses import dataclass, asdict, replace
from pathlib import Path
from threading import Event

import config
from core.league_lane import LaneContext
from core.league_replay import MinimapDetector, bounds, finite, local_file, read_json
from core.league_voice import LeagueAnnouncement


def valid_position(value):
    return isinstance(value,(tuple,list)) and len(value)==2 and all(finite(n) and 0<=n<=1 for n in value)


@dataclass(frozen=True)
class VisualObservation:
    champion: str
    team: str
    location: str
    confidence: float
    visible: bool
    timestamp: float
    source: str
    position: tuple
    box: tuple
    assigned_role: str | None = None
    assigned_lane: str | None = None
    in_assigned_lane: bool | None = None
    near_player: bool | None = None
    distance: float | None = None
    first_seen_at: float | None = None
    confidence_kind: str = "template similarity, not calibrated probability"
    identity_margin: float = 0.
    ambiguous: bool = False
    player_position_source: str = "unverified"
    detected_ns: int = 0
    first_detection_ns: int = 0
    confirmed_ns: int = 0

    def to_dict(self):
        return asdict(self)


class VisualCalibration:
    def __init__(self, spec, directory):
        if not isinstance(spec,dict):
            raise ValueError("Calibration must be a JSON object")
        self.spec = spec
        self.directory = Path(directory)
        self.reference = tuple(spec.get("reference_size",()))
        if len(self.reference)!=2 or any(type(n) is not int or not 100<=n<=7680 for n in self.reference):
            raise ValueError("Set the measured reference video resolution")
        self.minimap = bounds(spec.get("minimap"))
        self.fps = spec.get("fps",10)
        self.hud_scale = spec.get("hud_scale",1.)
        self.radius = spec.get("proximity_radius",.12)
        self.cooldown = spec.get("alert_cooldown",8.)
        self.threshold = spec.get("threshold",.92)
        self.confirmation_policy=spec.get("confirmation_policy","strict3")
        if self.confirmation_policy not in ("strict3","two_validated","adaptive"):
            raise ValueError("Confirmation policy must be strict3, two_validated or adaptive")
        for value,lo,hi,name in ((self.fps,1,30,"FPS"),(self.hud_scale,.5,2,"HUD scale"),
                                 (self.radius,.01,.5,"proximity radius"),(self.cooldown,0,120,"cooldown"),
                                 (self.threshold,.8,1,"confidence threshold")):
            if not finite(value) or not lo<=value<=hi:
                raise ValueError(f"Invalid {name}")
        self.orientation = spec.get("team_orientation","unknown")
        if self.orientation not in ("blue","red","unknown"):
            raise ValueError("Team orientation must be blue, red or unknown; it never implicitly mirrors pixels")
        self.muted = spec.get("alerts_muted",True)
        if type(self.muted) is not bool:
            raise ValueError("alerts_muted must be boolean")
        self.fallback = spec.get("fallback_role","UNKNOWN")
        from core.league_lane import ROLES
        if self.fallback not in (*ROLES,"UNKNOWN"):
            raise ValueError("Invalid explicit fallback position")
        self.viewport = bounds(spec["viewport"]) if spec.get("viewport") is not None else None
        for key,limit in (("hud_exclusions",30),("blockers",4),("regions",30),("roster",20)):
            if not isinstance(spec.get(key,[]),list) or len(spec.get(key,[]))>limit:
                raise ValueError(f"Invalid or oversized {key} list")
        if not isinstance(spec.get("viewport_nameplates",{}),dict):
            raise ValueError("viewport_nameplates must be an object")
        if spec.get("enemy_bar_color","red") not in ("red","yellow"):
            raise ValueError("Calibrate the enemy health-bar color as red or yellow")
        self.exclusions = [bounds(box) for box in spec.get("hud_exclusions",[])]
        self.blockers = spec.get("blockers",[])
        roster_names={item.get("champion") for item in spec.get("roster",[]) if isinstance(item,dict)}
        self.viewport_enabled=bool(self.viewport and self.blockers and roster_names
                                   and set(spec.get("viewport_nameplates",{}))==roster_names)
        self.regions = []
        for item in spec.get("regions",[])[:30]:
            if not isinstance(item,dict) or not isinstance(item.get("label"),str) or not 1<=len(item["label"])<=80:
                raise ValueError("Invalid named minimap region")
            if item.get("lane") not in (None,"TOP","MIDDLE","BOTTOM"):
                raise ValueError("A minimap region's lane must be TOP, MIDDLE, BOTTOM or null")
            self.regions.append((item["label"],bounds(item.get("bounds")),item.get("lane")))

    @classmethod
    def load(cls,path):
        path=local_file(path,purpose="Calibration file")
        return cls(read_json(path,65536),path.parent)

    def rois(self):
        result={"minimap":self.minimap}
        if self.viewport_enabled:
            result["viewport"]=self.viewport
        for index,blocker in enumerate(self.blockers):
            if not isinstance(blocker,dict):
                raise ValueError("Invalid scoreboard/occlusion blocker")
            result[f"blocker:{index}"]=bounds(blocker.get("region"))
        return result

    def validate_size(self,width,height):
        if min(width,height)<=0 or abs((width/height)/(self.reference[0]/self.reference[1])-1)>.02:
            raise ValueError("Resolution aspect ratio changed; recalibrate instead of guessing region coordinates")

    def regions_from_frame(self,frame):
        h,w=frame.shape[:2]
        self.validate_size(w,h)
        return {name:frame[round(y*h):round((y+rh)*h),round(x*w):round((x+rw)*w)]
                for name,(x,y,rw,rh) in self.rois().items()}

    def locate(self,position,lane):
        matches=[(name,role) for name,(x,y,w,h),role in self.regions if x<=position[0]<=x+w and y<=position[1]<=y+h]
        if len(matches)!=1:
            return "the minimap",None
        name,role=matches[0]
        return name,(role==lane.assigned_lane) if role in ("TOP","MIDDLE","BOTTOM") and lane.assigned_lane else None

    def offline_lane(self):
        role=self.spec.get("assigned_role",self.fallback)
        from core.league_lane import ROLES
        role=role if role in ROLES else None
        mode=self.spec.get("game_mode")
        number=self.spec.get("map_number")
        supported=mode=="CLASSIC" and number==11
        lane={"UTILITY":"BOTTOM","BOTTOM":"BOTTOM","TOP":"TOP","MIDDLE":"MIDDLE"}.get(role) if supported else None
        return LaneContext(role,"operator-labelled recording",lane,mode,number,
                           {"blue":"ORDER","red":"CHAOS"}.get(self.orientation,""),
                           self.spec.get("player_champion",""),1,supported)


class TemporalConfirmation:
    def __init__(self,fps=10,frames=3,policy="strict3"):
        if policy not in ("strict3","two_validated","adaptive"):
            raise ValueError("Confirmation policy must be strict3, two_validated or adaptive")
        self.policy=policy
        self.frames=frames
        self.gap=max(.3,2/fps)
        self.history={}
        self.epoch=None
        self.last_at=None
        self.first_seen={}
        self.first_ns={}

    def reset(self,epoch):
        self.history.clear();self.first_seen.clear();self.first_ns.clear();self.epoch=epoch;self.last_at=None

    def update(self,observations,at,epoch):
        if self.epoch!=epoch or self.last_at is not None and at<=self.last_at:
            self.reset(epoch)
        self.last_at=at
        current={(item.champion,item.source):item for item in observations
                 if item.visible and valid_position(item.position) and finite(item.confidence)
                 and finite(item.timestamp) and abs(at-item.timestamp)<=.00051}
        self.history={key:value for key,value in self.history.items() if key in current}
        self.first_seen={key:value for key,value in self.first_seen.items() if key in current}
        self.first_ns={key:value for key,value in self.first_ns.items() if key in current}
        result=[]
        for key,item in current.items():
            samples=self.history.setdefault(key,deque(maxlen=self.frames))
            if samples and (at-samples[-1].timestamp>self.gap or math.dist(item.position,samples[-1].position)>.15):
                samples.clear()
                self.first_seen.pop(key,None)
                self.first_ns.pop(key,None)
            self.first_seen.setdefault(key,item.timestamp)
            self.first_ns.setdefault(key,item.detected_ns)
            samples.append(item)
            fast=False
            if self.policy!="strict3" and len(samples)>=2:
                pair=list(samples)[-2:]
                score,margin=(.90,.15) if self.policy=="two_validated" else (.94,.20)
                fast=(math.dist(pair[0].position,pair[1].position)<=.025
                      and all(not s.ambiguous and s.confidence>=score and finite(s.identity_margin)
                              and s.identity_margin>=margin for s in pair))
            if fast or self.policy!="two_validated" and len(samples)>=self.frames:
                used=list(samples)[-2:] if fast else samples
                result.append(replace(item,confidence=min(s.confidence for s in used),first_seen_at=self.first_seen[key],
                                      first_detection_ns=self.first_ns[key],confirmed_ns=time.perf_counter_ns() if item.detected_ns else 0))
        return tuple(result)


class ViewportMatcher:
    """Champion-nameplate + nearby enemy health bar. No 3D/skin identity claim."""
    def __init__(self,profile):
        import cv2
        import numpy as np
        self.cv2,self.np,self.profile=cv2,np,profile
        self.templates={}
        if not profile.viewport_enabled:
            return
        refwidth=round(profile.viewport[2]*profile.reference[0])
        self.target_width=min(960,refwidth)
        self.scale=min(1,960/refwidth)
        roster={item["champion"] for item in profile.spec.get("roster",[])}
        for name,files in profile.spec.get("viewport_nameplates",{}).items():
            if name not in roster or not isinstance(files,list) or not 1<=len(files)<=4:
                raise ValueError("Viewport references must identify a known roster champion")
            self.templates[name]=[]
            for filename in files:
                path=local_file(profile.directory/filename)
                if path.stat().st_size>1_000_000:
                    raise ValueError("Viewport reference is too large")
                image=cv2.imread(str(path),cv2.IMREAD_GRAYSCALE)
                if image is None or min(image.shape)<5 or max(image.shape)>256 or float(np.std(image))<8:
                    raise ValueError("Use a textured, tightly cropped champion-nameplate reference")
                image=cv2.resize(image,None,fx=self.scale*profile.hud_scale,fy=self.scale*profile.hud_scale)
                self.templates[name].append(image)

    def detect(self,image,enemy_names,at,lane):
        if not self.templates:
            return []
        cv2,np=self.cv2,self.np
        h,w=image.shape[:2]
        ratio=self.target_width/w
        image=cv2.resize(image,None,fx=ratio,fy=ratio)
        gray=cv2.cvtColor(image,cv2.COLOR_BGR2GRAY)
        hsv=cv2.cvtColor(image,cv2.COLOR_BGR2HSV)
        if self.profile.spec.get("enemy_bar_color","red")=="yellow":
            red=cv2.inRange(hsv,np.array([18,140,80]),np.array([38,255,255]))
        else:
            red=cv2.inRange(hsv,np.array([0,140,80]),np.array([10,255,255])) | cv2.inRange(hsv,np.array([170,140,80]),np.array([179,255,255]))
        _,_,stats,_=cv2.connectedComponentsWithStats(red)
        bars=[(x,y,bw,bh) for x,y,bw,bh,area in stats[1:] if 15<=bw<=240 and 1<=bh<=15 and bw>=bh*3 and area>=bw*.5]
        if not bars or len(bars)>64:
            return []
        # All coordinates below are local to the cropped viewport.
        output=[]
        for name in enemy_names:
            best=None
            for reference in self.templates.get(name,[]):
                th,tw=reference.shape
                if th>gray.shape[0] or tw>gray.shape[1]:continue
                # Search only beside actual enemy-colored health bars. This also
                # avoids scanning most HUD/terrain pixels for every champion.
                dx=max(80*self.scale*self.profile.hud_scale,tw)
                dy=50*self.scale*self.profile.hud_scale
                best_match=None
                for bx,by,bw,bh in bars:
                    left=max(0,math.floor(bx+bw/2-dx-tw/2));right=min(gray.shape[1],math.ceil(bx+bw/2+dx+tw/2))
                    top=max(0,math.floor(by-dy-th));bottom=min(gray.shape[0],math.ceil(by+dy))
                    search=gray[top:bottom,left:right]
                    if search.shape[0]<th or search.shape[1]<tw:continue
                    _,score,_,(x,y)=cv2.minMaxLoc(cv2.matchTemplate(search,reference,cv2.TM_CCOEFF_NORMED))
                    if best_match is None or score>best_match[0]:best_match=(score,x+left,y+top)
                if best_match is None:continue
                score,x,y=best_match
                if score<self.profile.threshold or not math.isfinite(score):continue
                cx,cy=(x+tw/2)/gray.shape[1],(y+th/2)/gray.shape[0]
                vx,vy,vw,vh=self.profile.viewport
                gx,gy=vx+cx*vw,vy+cy*vh
                if any(ex<=gx<=ex+ew and ey<=gy<=ey+eh for ex,ey,ew,eh in (*self.profile.exclusions,self.profile.minimap)):
                    continue
                nearby=any(abs((bx+bw/2)-(x+tw/2))<dx and abs(by-(y+th))<dy for bx,by,bw,bh in bars)
                if not nearby:continue
                patch=gray[y:y+th,x:x+tw]
                competitor=max((float(cv2.matchTemplate(patch,cv2.resize(other,(tw,th)),cv2.TM_CCOEFF_NORMED)[0,0])
                                for other_name,variants in self.templates.items() if other_name!=name for other in variants),default=-1)
                if not math.isfinite(competitor) or score-competitor<self.profile.spec.get("margin",.08):continue
                if best is None or score>best.confidence:
                    best=VisualObservation(name,"enemy","the game view",round(score,5),True,at,"viewport",(cx,cy),
                                           (x/gray.shape[1],y/gray.shape[0],tw/gray.shape[1],th/gray.shape[0]),
                                           lane.role,lane.assigned_lane)
            if best:output.append(best)
        return output


class LeagueVisualPipeline:
    def __init__(self,profile):
        import cv2
        self.profile=profile
        if any(not isinstance(item,dict) for item in profile.spec.get("roster",[])):
            raise ValueError("Provide the known ally/enemy champion roster")
        enemies=[item["champion"] for item in profile.spec.get("roster",[]) if item.get("side")=="enemy"]
        if not enemies:raise ValueError("Calibrate a known enemy roster")
        spec={**profile.spec,"target_champion":enemies[0]}
        self.minimap=MinimapDetector(spec,profile.directory,allow_any_enemy=True,scale_factor=profile.hud_scale)
        self.enemies=tuple(enemies)
        self.allies=frozenset(item["champion"] for item in self.minimap.roster if item["side"]=="ally")
        self.temporal=TemporalConfirmation(profile.fps,policy=profile.confirmation_policy)
        self.raw_observations=()
        self.viewport=ViewportMatcher(profile)
        self.blockers=[]
        for index,item in enumerate(profile.blockers):
            path=local_file(profile.directory/item["image"])
            if path.stat().st_size>1_000_000:raise ValueError("Occlusion reference too large")
            image=cv2.imread(str(path),cv2.IMREAD_GRAYSCALE)
            if image is None or min(image.shape)<5 or max(image.shape)>512 or float(image.std())<8:
                raise ValueError("Use a textured scoreboard/occlusion reference")
            self.blockers.append((f"blocker:{index}",image))

    def process(self,regions,lane,at):
        import cv2
        self.raw_observations=()
        if not lane.supported:
            self.temporal.reset(lane.epoch)
            return ()
        for key,reference in self.blockers:
            crop=regions.get(key)
            if crop is None:raise ValueError("Occlusion region missing; refusing viewport/HUD identities")
            gray=cv2.cvtColor(crop,cv2.COLOR_BGR2GRAY)
            gray=cv2.resize(gray,(round(self.profile.rois()[key][2]*self.profile.reference[0]),
                                   round(self.profile.rois()[key][3]*self.profile.reference[1])))
            if reference.shape[0]<=gray.shape[0] and reference.shape[1]<=gray.shape[1]:
                score=cv2.minMaxLoc(cv2.matchTemplate(gray,reference,cv2.TM_CCOEFF_NORMED))[1]
                if score>=.95:
                    self.temporal.reset(lane.epoch)
                    return ()
        raw=regions.get("minimap")
        if raw is None:raise ValueError("Calibrated minimap region missing")
        crop=cv2.resize(raw,(self.minimap.width,self.minimap.height),interpolation=cv2.INTER_AREA)
        crop=self.minimap.prepare(crop)  # Once per frame, shared by every champion.
        gray=cv2.cvtColor(crop,cv2.COLOR_BGR2GRAY)
        # Attachment swaps Yuumi's visible marker to her host's face. No
        # independent attachment/position evidence exists in these inputs.
        own=(self.minimap.match(crop,lane.player_champion,gray)
             if lane.player_champion in self.allies and lane.player_champion!="Yuumi" else None)
        if own and not valid_position(own.get("minimap_position")):own=None
        observations=[]
        for name in self.enemies:
            found=self.minimap.match(crop,name,gray)
            if found is None:continue
            position=found["minimap_position"]
            if not valid_position(position):continue
            location,in_lane=self.profile.locate(position,lane)
            distance=math.dist(position,own["minimap_position"]) if own else None
            x,y,w,h=found["pixel_box"]
            observations.append(VisualObservation(name,"enemy",location,found["template_similarity"],True,at,"minimap",
                position,(x/self.minimap.width,y/self.minimap.height,w/self.minimap.width,h/self.minimap.height),
                lane.role,lane.assigned_lane,in_lane,distance<=self.profile.radius if distance is not None else None,distance,
                identity_margin=found["ambiguity_margin"],player_position_source="verified own minimap icon" if own else "unverified"))
        if "viewport" in regions:
            observations.extend(self.viewport.detect(regions["viewport"],self.enemies,at,lane))
        # Two identities occupying the same icon are not two known champions.
        conflicts=set();overlaps=set()
        for i,item in enumerate(observations):
            for j,other in enumerate(observations[:i]):
                if item.source==other.source and item.champion!=other.champion:
                    x,y,w,h=item.box;a,b,c,d=other.box
                    overlap=max(0,min(x+w,a+c)-max(x,a))*max(0,min(y+h,b+d)-max(y,b))
                    if overlap>0:overlaps.update((i,j))
                    if overlap>min(w*h,c*d)*.5:conflicts.update((i,j))
        observations=[replace(item,ambiguous=True) if i in overlaps else item
                      for i,item in enumerate(observations) if i not in conflicts]
        if observations and (config.LEAGUE_LATENCY_METRICS_ENABLED or config.VOICE_LATENCY_METRICS_ENABLED):
            detected=time.perf_counter_ns()
            observations=[replace(item,detected_ns=detected) for item in observations]
        self.raw_observations=tuple(observations)
        return self.temporal.update(observations,at,lane.epoch)


class VisualAlertDispatcher:
    """Deterministic, current-only proposals to the existing one-slot voice policy."""
    def __init__(self,profile):
        self.profile=profile
        self.current={}
        self.cooldowns={}
        self.offered={}
        self.epoch=None
        self.sequence=0
        import uuid
        self.token=uuid.uuid4().hex[:8]
        self.muted=profile.muted
        self.enemy_names=frozenset(p["champion"] for p in profile.spec.get("roster",[]) if p["side"]=="enemy")
        self.episodes={}
        self.reserved={}
        self.spoken=set()
        self.retry_at={}
        self.states={}
        self.recent_spoken={}
        self.counts={name:0 for name in ("proposed","accepted","started","dropped")}

    def invalidate(self):
        for key in tuple(self.offered):
            self._drop(key)
            self.states[key]["cancel"].set()
        self.current.clear();self.episodes.clear();self.spoken.clear();self.recent_spoken.clear()

    def eligible(self,item):
        near=(item.near_player is True and finite(item.distance) and item.distance>=0
              and item.player_position_source=="verified own minimap icon")
        return near or item.in_assigned_lane is True or item.source=="viewport" and self.profile.viewport_enabled

    def _drop(self,key,now=None):
        entry=self.states.get(key)
        if not entry or entry["state"] in ("dropped","started"):return
        entry["state"]="dropped";self.counts["dropped"]+=1
        entry["cancel"].set()
        self.reserved.pop(entry["episode"],None)
        if now is not None:self.retry_at[entry["champion"]]=now+.25

    def lifecycle(self,announcement,state,now=None):
        """Thread owner serializes this; proposal/acceptance never spend cooldown."""
        now=time.monotonic() if now is None else now
        entry=self.states.get(announcement.event_key)
        if entry is None or announcement.epoch!=self.epoch:return False
        if state=="dropped":
            self._drop(announcement.event_key,now)
            self.retry_at[entry["champion"]]=max(self.retry_at.get(entry["champion"],now),announcement.not_before)
            return True
        if state not in ("accepted","started"):return False
        if entry["state"]==state:return True
        if entry["state"] in ("started","dropped"):return False
        if state=="accepted" and not self.is_current(announcement,now):
            self._drop(announcement.event_key,now);return False
        entry["state"]=state;self.counts[state]+=1
        if state=="started":
            self.cooldowns[entry["champion"]]=now
            self.spoken.add(entry["episode"])
            self.reserved.pop(entry["episode"],None)
            original=self.offered[announcement.event_key];key=(original.champion,original.source)
            current=self.current.get(key)
            if current:self.recent_spoken[key]=(now,current.position)
        return True

    def update(self,observations,epoch,now=None,*,live=False,approved=False):
        now=time.monotonic() if now is None else now
        if self.epoch!=epoch:
            self.invalidate()
            self.cooldowns.clear();self.offered.clear();self.states.clear();self.reserved.clear();self.retry_at.clear();self.epoch=epoch
        previous=self.current
        self.current={(item.champion,item.source):item for item in observations
                      if item.visible and item.team=="enemy" and item.champion in self.enemy_names
                      and item.source in ("minimap","viewport") and valid_position(item.position)
                      and finite(item.confidence) and finite(item.timestamp)
                      and 0<=now-item.timestamp<=config.LEAGUE_VISION_MAX_FRAME_AGE}
        episodes={}
        for key,item in self.current.items():
            old=previous.get(key)
            if (old and key in self.episodes and 0<=item.timestamp-old.timestamp<=config.LEAGUE_VISION_MAX_FRAME_AGE
                    and math.dist(old.position,item.position)<=.15
                    and old.first_seen_at==item.first_seen_at):episodes[key]=self.episodes[key]
            else:
                self.sequence+=1;episodes[key]=self.sequence
        self.episodes=episodes
        active=set(episodes.values());self.spoken.intersection_update(active)
        # Debounce a brief confirmation dropout ONLY for already spoken notices.
        # This never creates/extends a visible observation or queues old text.
        # Unstarted/dropped notices do not gain this suppression latch.
        self.recent_spoken={k:v for k,v in self.recent_spoken.items() if 0<=now-v[0]<=.5}
        for key,item in self.current.items():
            recent=self.recent_spoken.get(key)
            other_source=any(k[0]==item.champion and k[1]!=item.source for k in self.recent_spoken)
            if other_source or recent and math.dist(recent[1],item.position)<=.15:self.spoken.add(episodes[key])
            if episodes[key] in self.spoken:self.recent_spoken[key]=(now,item.position)
        for key,entry in tuple(self.states.items()):
            if entry["episode"] not in active or now-entry["proposed_at"]>config.LEAGUE_VISION_ALERT_LIFETIME:
                self._drop(key)
                # A playback already started is cancelled on lost visibility too.
                if entry["episode"] not in active:entry["cancel"].set()
        if self.muted or live and not approved:
            self.invalidate()
            return ()
        proposals=[]
        reserved_champions={entry["champion"] for entry in self.states.values() if entry["episode"] in self.reserved}
        for item in self.current.values():
            episode=self.episodes[item.champion,item.source]
            if (item.champion in reserved_champions or episode in self.spoken or now<self.retry_at.get(item.champion,-math.inf)
                    or item.confidence<self.profile.threshold or now-self.cooldowns.get(item.champion,-math.inf)<self.profile.cooldown):
                continue
            if not self.eligible(item):
                continue
            self.sequence+=1
            key=f"visual:{self.token}:{item.champion}:{self.sequence}"
            text=f"{item.champion} visible in {item.location}." if item.location!="the minimap" else f"{item.champion} visible on the minimap."
            cancel=Event()
            proposal_ns=time.perf_counter_ns()
            announcement=LeagueAnnouncement(text,key,now,config.LEAGUE_VISION_ALERT_LIFETIME,0,
                                            proposal_ns,epoch,item.champion,detected_ns=item.first_detection_ns or proposal_ns,
                                            cancel_event=cancel,first_raw_ns=item.first_detection_ns,confirmed_ns=item.confirmed_ns)
            self.offered[key]=item
            self.states[key]={"state":"proposed","episode":episode,"champion":item.champion,
                              "proposed_at":now,"cancel":cancel}
            self.reserved[episode]=key;self.counts["proposed"]+=1
            reserved_champions.add(item.champion)
            proposals.append(announcement)
        while len(self.offered)>64:
            key=next(iter(self.offered));self._drop(key);self.offered.pop(key);self.states.pop(key,None)
        return tuple(proposals)

    def is_current(self,announcement,now=None):
        now=time.monotonic() if now is None else now
        original=self.offered.get(announcement.event_key)
        current=self.current.get((original.champion,original.source)) if original else None
        entry=self.states.get(announcement.event_key)
        return bool(not self.muted and announcement.epoch==self.epoch and current is not None
                    and entry and entry["state"]!="dropped" and not entry["cancel"].is_set()
                    and self.episodes.get((original.champion,original.source))==entry["episode"]
                    and current.confidence>=self.profile.threshold
                    and self.eligible(current)
                    and current.location==original.location and 0<=now-current.timestamp<=config.LEAGUE_VISION_MAX_FRAME_AGE
                    and 0<=now-announcement.captured_at<=announcement.lifetime)
