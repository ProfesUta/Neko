"""Automatic offline icon preparation; no model, manual crop or live polling."""
from pathlib import Path
import json

import config
from core.cancellation import check_cancelled
from core.league_replay import local_file,LocalFileError
from core.league_visual import VisualCalibration


def prepare_icons(calibration, cancellation=None, *, knowledge=None, replace=False):
    path=local_file(calibration,purpose="Calibration file")
    original=path.read_bytes()
    if len(original)>65536:raise ValueError("Oversized calibration")
    spec=json.loads(original)
    profile=VisualCalibration(spec,path.parent)
    roster=spec.get("roster",[])
    if (not 2<=len(roster)<=20 or any(not isinstance(p,dict) or not isinstance(p.get("champion"),str)
            or not 1<=len(p["champion"])<=80 or p.get("side") not in ("ally","enemy") for p in roster)):
        raise ValueError("Set the known ally/enemy champion roster before getting official icons")
    names=[p["champion"] for p in roster]
    if len(set(names))!=len(names):raise ValueError("Repeated champions need separate team-marker calibration")
    existing=spec.get("templates",{})
    if not isinstance(existing,dict):raise ValueError("templates must be an object")
    templates={};missing=[]
    for name in names:
        check_cancelled(cancellation)
        filename=existing.get(name)
        if not replace and isinstance(filename,str):
            try:
                local_file(path.parent/filename)
                templates[name]=filename
                continue
            except LocalFileError:pass
        missing.append(name)
    if not missing and set(existing)==set(names):
        return {"status":"ready","prepared":0,"preserved":len(names),"manual_cropping_required":False}
    own_service=knowledge is None
    if own_service:
        from core.league_knowledge import LeagueKnowledge
        from core.league_champ_select import ChampionNames
        knowledge=LeagueKnowledge(ChampionNames())
    try:
        import cv2
        import numpy as np
        from core.league_visual_evaluation import save_json
        x,y,w,h=profile.minimap;rw,rh=profile.reference
        edge=min(round((x+w)*rw)-round(x*rw),round((y+h)*rh)-round(y*rh))
        # Estimate the inner portrait's reference size; the matcher applies HUD scale.
        size=max(8,min(64,round(edge*.0625/profile.hud_scale)))
        source_info=spec.get("_template_sources",{})
        if not isinstance(source_info,dict):raise ValueError("Icon source metadata must be an object")
        sources=dict(source_info)
        for name in missing:
            check_cancelled(cancellation)
            source=knowledge.champion_icon(name,cancellation,config.DATA_DIR/"league-vision"/"references")
            image=cv2.imread(str(source),cv2.IMREAD_COLOR)
            if image is None:raise ValueError(f"Cannot decode the cached official icon for {name}")
            ih,iw=image.shape[:2];pad=round(min(ih,iw)*.175)
            image=cv2.resize(image[pad:ih-pad,pad:iw-pad],(size,size),interpolation=cv2.INTER_AREA)
            if float(np.std(image.astype("float32"),axis=(0,1)).mean())<8:
                raise ValueError(f"Official icon for {name} has insufficient texture")
            version=source.parent.name.removeprefix("riot-")
            target=path.parent/"references"/"official-minimap"/f"{version}-{source.stem}-{size}.png"
            target.parent.mkdir(parents=True,exist_ok=True)
            if not cv2.imwrite(str(target),image):raise ValueError(f"Could not cache the minimap icon for {name}")
            templates[name]=target.relative_to(path.parent).as_posix()
            sources[name]={"kind":"official static portrait; automatically sized, accuracy unmeasured",
                           "source":f"https://ddragon.leagueoflegends.com/cdn/{version}/img/champion/{source.name}",
                           "reference_pixels":size,"manual_cropping_required":False}
        check_cancelled(cancellation)
        if path.read_bytes()!=original:
            raise ValueError("Calibration changed while icons were prepared; cached icons retained. Retry with the new settings")
        spec["templates"]={name:templates[name] for name in names}
        spec["_template_sources"]={name:sources[name] for name in names if name in sources}
        save_json(path,spec)
        return {"status":"ready","prepared":len(missing),"preserved":len(names)-len(missing),
                "patch":knowledge.version,"manual_cropping_required":False}
    finally:
        if own_service:knowledge.close()
