import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace

import httpx
import numpy as np
import pytest

cv2=pytest.importorskip("cv2")

import config
from core.cancellation import OperationCancelled
from core.league_icons import prepare_icons
from core.league_knowledge import LeagueKnowledge
from core.league_visual_evaluation import save_json,analyze_recording


@pytest.fixture
def icon_setup(tmp_path,monkeypatch):
    monkeypatch.setattr(config,"LEAGUE_STATIC_DOWNLOADS_ENABLED",True)
    rng=np.random.default_rng(444)
    pictures={name:cv2.resize(rng.integers(0,255,(20,20,3),dtype=np.uint8),(120,120),interpolation=cv2.INTER_NEAREST)
              for name in ("Ahri","Lee Sin")}
    encoded={name:cv2.imencode(".png",image)[1].tobytes() for name,image in pictures.items()}
    calls=[]
    def response(request):
        calls.append(str(request.url))
        assert request.url.host=="ddragon.leagueoflegends.com" and request.method=="GET"
        return httpx.Response(200,content=encoded["Lee Sin" if request.url.path.endswith("LeeSin.png") else "Ahri"])
    service=LeagueKnowledge(SimpleNamespace(names={}),tmp_path/"knowledge",httpx.Client(transport=httpx.MockTransport(response)))
    service.version="99.1.1"
    service._save("99.1.1-champion.json",{"data":{
        "LeeSin":{"name":"Lee Sin","image":{"full":"LeeSin.png"}},"Ahri":{"name":"Ahri","image":{"full":"Ahri.png"}}}})
    spec={"reference_size":[128,128],"minimap":[0,0,1,1],"perspective":"player","fog_of_war":"player",
          "game_mode":"CLASSIC","map_number":11,"assigned_role":"MIDDLE","player_champion":"Ahri",
          "roster":[{"champion":"Lee Sin","side":"enemy","position":"JUNGLE"},{"champion":"Ahri","side":"ally","position":"MIDDLE"}],
          "templates":{}}
    path=save_json(tmp_path/"profile"/"calibration.json",spec)
    yield service,path,calls,pictures
    service.close()


def test_missing_icons_download_once_and_repeat_preparation_is_offline(icon_setup,monkeypatch):
    service,path,calls,_=icon_setup
    result=prepare_icons(path,knowledge=service)
    assert result["prepared"]==2 and result["manual_cropping_required"] is False and len(calls)==2
    spec=json.loads(path.read_text())
    assert spec["minimap"]==[0,0,1,1] and spec["assigned_role"]=="MIDDLE"
    assert set(spec["templates"])=={"Lee Sin","Ahri"}
    for name,filename in spec["templates"].items():
        assert cv2.imread(str(path.parent/filename)).shape==(8,8,3)
        assert spec["_template_sources"][name]["source"].startswith("https://ddragon.leagueoflegends.com/")
    original=path.read_bytes()
    monkeypatch.setattr(service,"champion_icon",lambda *args:pytest.fail("Do not fetch ready references"))
    second=prepare_icons(path,knowledge=service)
    assert second["prepared"]==0 and second["preserved"]==2 and path.read_bytes()==original


def test_cached_official_icons_work_without_network_or_static_download_permission(icon_setup,monkeypatch):
    service,path,calls,_=icon_setup
    reference=service.champion_icon("LeeSin")
    assert reference.is_file() and len(calls)==1
    monkeypatch.setattr(config,"LEAGUE_STATIC_DOWNLOADS_ENABLED",False)
    assert service.champion_icon("lee sin")==reference and len(calls)==1
    with pytest.raises(ValueError,match="downloads are disabled"):service.champion_icon("Ahri")


def test_existing_recording_crops_are_preserved_unless_replacement_is_explicit(icon_setup):
    service,path,calls,pictures=icon_setup
    native=path.parent/"native.png";cv2.imwrite(str(native),pictures["Ahri"][:16,:16])
    spec=json.loads(path.read_text());spec["templates"]={"Ahri":"native.png"};save_json(path,spec)
    before=native.read_bytes()
    assert prepare_icons(path,knowledge=service)["prepared"]==1
    assert json.loads(path.read_text())["templates"]["Ahri"]=="native.png" and native.read_bytes()==before
    result=prepare_icons(path,knowledge=service,replace=True)
    assert result["prepared"]==2 and json.loads(path.read_text())["templates"]["Ahri"]!="native.png"
    assert native.read_bytes()==before


@pytest.mark.parametrize("payload",[b"not a PNG",b"x"*1_000_001])
def test_bad_or_oversized_download_does_not_overwrite_calibration(icon_setup,payload):
    service,path,_,_=icon_setup;before=path.read_bytes()
    service._http.close();service._http=httpx.Client(transport=httpx.MockTransport(lambda request:httpx.Response(200,content=payload)))
    with pytest.raises(ValueError):prepare_icons(path,knowledge=service)
    assert path.read_bytes()==before and not list(service.root.rglob("*.tmp"))


def test_network_failure_reports_honestly_and_preserves_profile(icon_setup):
    service,path,_,_=icon_setup;before=path.read_bytes()
    service._http.close();service._http=httpx.Client(transport=httpx.MockTransport(lambda request:httpx.Response(503)))
    with pytest.raises(ValueError,match="download failed"):prepare_icons(path,knowledge=service)
    assert path.read_bytes()==before


def test_cancellation_and_concurrent_edit_preserve_user_settings(icon_setup,monkeypatch):
    service,path,_,_=icon_setup;before=path.read_bytes();stop=Event();stop.set()
    with pytest.raises(OperationCancelled):prepare_icons(path,stop,knowledge=service)
    assert path.read_bytes()==before
    original=service.champion_icon
    def changed(*args):
        result=original(*args)
        spec=json.loads(path.read_text());spec["threshold"]=.95;save_json(path,spec)
        return result
    monkeypatch.setattr(service,"champion_icon",changed)
    with pytest.raises(ValueError,match="Calibration changed"):prepare_icons(path,knowledge=service)
    assert json.loads(path.read_text())["threshold"]==.95


def test_concurrent_identical_icon_requests_coalesce(icon_setup):
    service,_,calls,_=icon_setup
    with ThreadPoolExecutor(4) as pool:paths=list(pool.map(lambda _:service.champion_icon("Lee Sin"),range(8)))
    assert len(set(paths))==1 and len(calls)==1


def test_catalog_cannot_supply_a_traversing_icon_path(icon_setup):
    service,_,calls,_=icon_setup
    service._save("99.1.1-champion.json",{"data":{"Ahri":{"name":"Ahri","image":{"full":"../../private.png"}}}})
    with pytest.raises(ValueError,match="No official champion icon"):service.champion_icon("Ahri")
    assert calls==[]


def test_video_analysis_automatically_prepares_missing_icons_without_a_review_gate(icon_setup,tmp_path):
    service,path,calls,_=icon_setup
    video=tmp_path/"clip.avi";writer=cv2.VideoWriter(str(video),cv2.VideoWriter_fourcc(*"MJPG"),10,(128,128))
    assert writer.isOpened()
    for _ in range(4):writer.write(np.zeros((128,128,3),np.uint8))
    writer.release()
    report=analyze_recording(video,path,tmp_path/"analysis",knowledge=service)
    assert report["reference_setup"]["prepared"]==2 and report["analyzed_frames"]==4
    assert report["accuracy"] is None and report["candidates"]==[] and len(calls)==2


def test_editor_fetches_icons_on_existing_background_worker(icon_setup,qapp,wait_until):
    from gui.league_calibration import LeagueCalibrationDialog
    service,path,calls,_=icon_setup
    dialog=LeagueCalibrationDialog(path,league=SimpleNamespace(knowledge=service))
    dialog.get_icons()
    assert dialog.worker is not None and dialog.editor.isReadOnly()
    wait_until(lambda:dialog.worker is None)
    assert not dialog.editor.isReadOnly() and len(calls)==2
    assert "Champion icons ready" in dialog.message.text()
    assert set(json.loads(dialog.editor.toPlainText())["templates"])=={"Ahri","Lee Sin"}
    dialog.close();dialog.deleteLater()
