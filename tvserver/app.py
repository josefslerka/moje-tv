"""Webový server: přehrávač, M3U pro Apple TV a jednoduché API."""
from __future__ import annotations

import asyncio
import dataclasses
import gzip
import json
import time
from urllib.parse import quote

import httpx
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import airplay, captions, catalog, checker, db, epg, m3u, proxy, recordings, webstream, wiki, wikiweb
from .netinfo import lan_addresses
from .config import Config, load
from .caption_usage import CaptionUsage

log = logging.getLogger("tvserver")
STATIC = Path(__file__).with_name("static")


def _due(last: str | None, hours: float) -> bool:
    if not last:
        return True
    return datetime.now(timezone.utc) - datetime.fromisoformat(last) >= timedelta(hours=hours)


async def _scheduler(con, cfg: Config) -> None:
    """Každou hodinu se podívá, jestli už je čas na kontrolu streamů a obnovu programu."""
    while True:
        try:
            if _due(db.get_meta(con, "last_check"), cfg.check_interval_hours):
                log.info("Kontrola streamů: %s", await checker.run_check(con, cfg))
        except Exception:  # noqa: BLE001
            log.exception("Kontrola streamů selhala")
        try:
            if _due(db.get_meta(con, "last_catalog"), cfg.catalog_interval_hours):
                log.info("Katalog: %s", await catalog.refresh(con, cfg))
        except Exception:  # noqa: BLE001
            log.exception("Obnova katalogu selhala")
        try:
            if _due(db.get_meta(con, "last_epg"), cfg.epg_interval_hours):
                log.info("Program: %s", await epg.refresh(con, cfg))
        except Exception:  # noqa: BLE001
            log.exception("Obnova programu selhala")
        try:
            if _due(db.get_meta(con, "last_wiki"), cfg.wiki_interval_hours):
                log.info("Wiki: %s", await _wiki_job(con, cfg))
        except Exception:  # noqa: BLE001
            log.exception("Generování wiki selhalo")
        await asyncio.sleep(3600)


_wiki_lock = asyncio.Lock()


async def _wiki_job(con, cfg: Config) -> dict:
    async with _wiki_lock:
        return await wiki.build(con, cfg)


def _wiki_links(con, cfg: Config) -> dict[str, dict]:
    """Stránka kanálu ve wiki: adresa v aplikaci a v Obsidianu, podle adresy streamu."""
    try:
        rows = con.execute("SELECT url, page FROM wiki_links").fetchall()
    except Exception:  # noqa: BLE001 - tabulka vznikne až při prvním generování wiki
        return {}
    return {r["url"]: {"wiki": wikiweb.page_url(r["page"]), "obsidian": _obsidian(cfg, r["page"])}
            for r in rows if not r["page"].startswith("obsidian:")}


def _obsidian(cfg: Config, page: str) -> str:
    return f"obsidian://open?vault={quote(cfg.obsidian_vault)}&file={quote(cfg.obsidian_prefix + '/' + page)}"


def _channel_json(r) -> dict:
    return {"id": r["id"], "name": m3u.clean_name(r["name"]), "group": r["grp"] or "Ostatní", "logo": r["logo"],
            "url": r["url"], "status": r["status"], "last_ok": r["last_ok"], "favorite": bool(r["favorite"]),
            "web": webstream.is_web(r["url"])}


class UrlBody(BaseModel):
    url: str


class UrlsBody(BaseModel):
    urls: list[str]


class PatchBody(BaseModel):
    name: str | None = None
    group: str | None = None
    enabled: bool | None = None
    favorite: bool | None = None
    epg: str | None = None


class OrderItem(BaseModel):
    id: int
    group: str


class OrderBody(BaseModel):
    items: list[OrderItem]


class RenameBody(BaseModel):
    old: str
    new: str


class AddBody(BaseModel):
    url: str
    name: str = ""
    group: str = ""
    skip_test: bool = False


class CaptionSessionBody(BaseModel):
    channel_id: int
    language: Literal["en", "cs", "auto"] = "en"
    translation: Literal["cs"] | None = None
    translation_provider: Literal["local", "openrouter"] = "local"


class AirPlayBody(BaseModel):
    channel_id: int
    mode: Literal["asr", "translate-cs", "translate-cs-api"] | None = None


class RecordingBody(BaseModel):
    channel_id: int
    video: bool = True
    text_mode: Literal["asr", "translate-cs", "translate-cs-api"] | None = "asr"
    max_seconds: int = Field(default=7200, ge=60, le=7200)


def create_app(cfg: Config | None = None, scheduler: bool = True) -> FastAPI:
    cfg = cfg or load()
    con = db.connect(cfg.database)

    clients: dict = {}
    caption_usage = CaptionUsage(con)
    caption_service = captions.CaptionService(cfg.caption_runtime if cfg.caption_runtime.is_absolute()
                                              else cfg.base_dir / cfg.caption_runtime, usage=caption_usage)
    airplay_service = airplay.AirPlayService(caption_service)
    recording_service = recordings.RecordingService(con, cfg.database.parent / "recordings", caption_service)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        clients["proxy"] = httpx.AsyncClient(follow_redirects=True, timeout=20,
                                             limits=httpx.Limits(max_connections=50))
        recording_service.recover()
        task = asyncio.create_task(_scheduler(con, cfg)) if scheduler else None
        try:
            yield
        finally:
            if task:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await airplay_service.stop()
            await recording_service.close()
            await caption_service.close()
            await clients["proxy"].aclose()
            con.close()

    app = FastAPI(title="Osobní IPTV", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    app.state.con, app.state.cfg = con, cfg
    app.state.captions = caption_service
    app.state.recordings = recording_service

    # Všechny endpointy jsou async, takže běží v jednom vlákně se schedulerem
    # a sdílené spojení do SQLite se nepoužívá souběžně z více vláken.

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/sprava", include_in_schema=False)
    async def sprava():
        return FileResponse(STATIC / "sprava.html")

    @app.get("/moje", include_in_schema=False)
    async def moje():
        return FileResponse(STATIC / "moje.html")

    @app.get("/zaznamy", include_in_schema=False)
    async def zaznamy():
        return FileResponse(STATIC / "zaznamy.html")

    @app.get("/wiki", include_in_schema=False)
    @app.get("/wiki/{page:path}", include_in_schema=False)
    async def wiki_page(page: str = ""):
        return FileResponse(STATIC / "wiki.html")

    @app.get("/api/wiki/page")
    async def api_wiki_page(path: str = ""):
        r = wikiweb.render(cfg.wiki_dir, path, cfg.obsidian_prefix, cfg.obsidian_vault, cfg.port)
        if not r:
            raise HTTPException(404, "Stránka ve wiki není")
        return r

    @app.get("/api/wiki/tree")
    async def api_wiki_tree():
        return wikiweb.tree(cfg.wiki_dir) if cfg.wiki_dir.exists() else []

    @app.get("/api/wiki/search")
    async def api_wiki_search(q: str = ""):
        return wikiweb.search(cfg.wiki_dir, q) if cfg.wiki_dir.exists() else []

    @app.get("/nastaveni", include_in_schema=False)
    async def nastaveni():
        return FileResponse(STATIC / "nastaveni.html")

    # Program pro nově přidané kanály se stáhne chvíli po posledním přidání,
    # aby se při přidávání několika kanálů za sebou nestahoval pokaždé znovu.
    pending = {"task": None}

    def epg_soon(delay: float = 20):
        if pending["task"]:
            pending["task"].cancel()
        async def run():
            await asyncio.sleep(delay)
            await epg.refresh(con, cfg)
        pending["task"] = asyncio.create_task(run())

    @app.get("/playlist.m3u")
    async def playlist(request: Request, group: str = "", favorites: bool = False):
        """Celý seznam, nebo jen jedna skupina (?group=Zprávy) či oblíbené (?favorites=true)."""
        rows = db.visible(con, cfg.hide_after_failures, group, favorites)
        base = str(request.base_url).rstrip("/")
        entries = [db.to_entry(r) for r in rows]
        # Živé vysílání z YouTube a Twitche přehrávače neumí, dostanou adresu přes server.
        entries = [dataclasses.replace(e, url=f"{base}/live?u={quote(e.url, safe='')}") if webstream.is_web(e.url)
                   else e for e in entries]
        body = m3u.render(entries, epg_url=base + "/epg.xml.gz")
        return Response(body, media_type="audio/x-mpegurl",
                        headers={"Content-Disposition": 'inline; filename="playlist.m3u"'})

    @app.get("/api/channels")
    async def channels(all: bool = False):
        links = _wiki_links(con, cfg)
        if not all:
            return [_channel_json(r) | links.get(r["url"], {"wiki": "", "obsidian": ""}) for r in db.visible(con, cfg.hide_after_failures)]
        epg_rows = {r["channel_id"]: r for r in con.execute("SELECT * FROM epg_status")}
        nn = epg.now_next(con, [r["id"] for r in db.all_channels(con)])
        out = []
        for r in db.all_channels(con):
            e = epg_rows.get(r["id"])
            out.append(_channel_json(r) | links.get(r["url"], {"wiki": "", "obsidian": ""}) | {
                "group": r["grp"], "raw_name": r["name"], "enabled": bool(r["enabled"]),
                "fail_count": r["fail_count"], "hidden": r["fail_count"] >= cfg.hide_after_failures,
                "detail": r["detail"], "last_check": r["last_check"], "tvg_id": r["tvg_id"],
                "source": r["source"], "epg_override": r["epg"],
                "restream": checker.is_restream(r["url"], cfg) or not checker.is_allowed(r["url"], cfg),
                "epg_how": e["how"] if e else None, "epg_count": e["count"] if e else 0,
                "epg_source": e["source"] if e else "", "now": (nn.get(r["id"]) or {}).get("now"),
            })
        return out

    @app.get("/api/captions/status")
    async def caption_status():
        return caption_service.status()

    @app.get("/api/recordings")
    async def recording_list():
        return JSONResponse({"recordings": recording_service.list(), "storage": recording_service.storage()},
                            headers={"Cache-Control": "no-store"})

    @app.post("/api/recordings")
    async def recording_start(body: RecordingBody):
        row = con.execute("SELECT id,name,url FROM channels WHERE id=? AND enabled=1", (body.channel_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Kanál neexistuje.")
        source = f"http://127.0.0.1:{cfg.port}" + proxy.link(con, row["url"])
        try:
            return await recording_service.start(dict(row), source, video=body.video,
                                                 text_mode=body.text_mode, max_seconds=body.max_seconds)
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        except recordings.RecordingError as error:
            raise HTTPException(409, str(error)) from error
        except captions.CaptionError as error:
            raise HTTPException(503, str(error)) from error
        except OSError as error:
            raise HTTPException(503, "Záznam nelze uložit na disk.") from error

    @app.post("/api/recordings/{rid}/stop")
    async def recording_stop(rid: str):
        if recording_service.get(rid) is None:
            raise HTTPException(404, "Záznam neexistuje.")
        recording_service.stop(rid)
        return recording_service.get(rid)

    @app.delete("/api/recordings/{rid}", status_code=204)
    async def recording_delete(rid: str):
        try:
            recording_service.delete(rid)
        except KeyError:
            raise HTTPException(404, "Záznam neexistuje.")
        except recordings.RecordingError as error:
            raise HTTPException(409, str(error)) from error
        return Response(status_code=204)

    @app.get("/api/recordings/{rid}/video")
    async def recording_video(rid: str, download: bool = False):
        item = recording_service.get(rid)
        if not item or not item["has_video"]:
            raise HTTPException(404, "Video ještě není k dispozici.")
        filename = f'{item["channel_name"]} {item["started_at"][:19].replace(":", "-")}.mp4' if download else None
        return FileResponse(recording_service.folder / rid / "video.mp4", media_type="video/mp4", filename=filename)

    @app.get("/api/recordings/{rid}/text.{extension}")
    async def recording_text(rid: str, extension: Literal["txt", "srt", "vtt"]):
        item = recording_service.get(rid)
        if not item:
            raise HTTPException(404, "Záznam neexistuje.")
        media_type = {"txt": "text/plain", "srt": "application/x-subrip", "vtt": "text/vtt"}[extension]
        headers = {"Cache-Control": "no-store"}
        if extension != "vtt":
            name = quote(f'{item["channel_name"]} {item["started_at"][:19].replace(":", "-")}.{extension}', safe="")
            headers["Content-Disposition"] = f"attachment; filename*=UTF-8''{name}"
        return Response(recording_service.export(rid, extension), media_type=media_type, headers=headers)

    @app.post("/api/airplay/sessions")
    async def airplay_start(body: AirPlayBody):
        row = con.execute("SELECT url FROM channels WHERE id=? AND enabled=1", (body.channel_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Kanál neexistuje.")
        base = lan_addresses(cfg.port).get("ip_url")
        if not base or cfg.host in {"127.0.0.1", "localhost", "::1"}:
            raise HTTPException(503, "Televize potřebuje adresu tohoto serveru v domácí síti.")
        source = proxy.link(con, row["url"])
        if body.mode is None:
            return {"state": "ready", "url": base + source, "session": None}
        try:
            session = await airplay_service.start(body.channel_id, f"http://127.0.0.1:{cfg.port}" + source, body.mode)
        except captions.CaptionBusy as error:
            raise HTTPException(429, str(error)) from error
        except (airplay.AirPlayError, captions.CaptionError) as error:
            raise HTTPException(503, str(error)) from error
        except (OSError, ValueError, TypeError) as error:
            raise HTTPException(503, "Příprava videa pro AirPlay není dostupná.") from error
        return {"state": session.state, "url": f"{base}/airplay/{session.id}/master.m3u8", "session": session.id}

    @app.get("/api/airplay/sessions/{sid}")
    async def airplay_status(sid: str):
        session = airplay_service.get(sid)
        if session is None:
            raise HTTPException(404, "Přenos už není aktivní.")
        session.touched = time.monotonic()
        return JSONResponse({"state": session.state, "error": session.error}, headers={"Cache-Control": "no-store"})

    @app.delete("/api/airplay/sessions/{sid}", status_code=204)
    async def airplay_stop(sid: str):
        await airplay_service.stop(sid)
        return Response(status_code=204)

    @app.api_route("/airplay/{sid}/{asset}", methods=["GET", "HEAD"], include_in_schema=False)
    async def airplay_media(sid: str, asset: str, request: Request):
        session = airplay_service.get(sid)
        if session is None:
            raise HTTPException(404, "Přenos už není aktivní.")
        media = session.media(asset)
        if media is None:
            if session.state == "error":
                raise HTTPException(503, session.error)
            raise HTTPException(404, "Část přenosu ještě není dostupná.")
        # Playlist polling alone can continue after the viewer pauses.
        if request.method == "GET" and asset.endswith(".ts"):
            session.touched = time.monotonic()
        body, content_type = media
        return Response(body, media_type=content_type,
                        headers={"Cache-Control": "no-store", "Access-Control-Allow-Origin": "*"})

    @app.get("/api/captions/usage")
    async def caption_costs():
        return JSONResponse(caption_usage.summary(), headers={"Cache-Control": "no-store"})

    @app.post("/api/captions/sessions")
    async def caption_session(body: CaptionSessionBody):
        row = con.execute("SELECT id FROM channels WHERE id = ? AND enabled = 1", (body.channel_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Kanál neexistuje.")
        try:
            sid = caption_service.create(body.channel_id, body.language, body.translation, body.translation_provider)
        except ValueError as error:
            raise HTTPException(422, str(error))
        except captions.CaptionBusy as error:
            raise HTTPException(429, str(error))
        except captions.CaptionError as error:
            raise HTTPException(503, str(error))
        return {"session": sid, "language": body.language, "translation": body.translation or False,
                "translation_provider": body.translation_provider if body.translation else None,
                "paid": bool(body.translation and body.translation_provider == "openrouter")}

    @app.delete("/api/captions/sessions/{sid}", status_code=204)
    async def caption_stop(sid: str):
        caption_service.remove(sid)
        return Response(status_code=204)

    @app.post("/api/captions/sessions/{sid}/audio")
    async def caption_audio(sid: str, start: float, end: float, request: Request):
        session = caption_service.sessions.get(sid)
        if not session:
            raise HTTPException(404, "Přepis už není aktivní.")
        if not con.execute("SELECT id FROM channels WHERE id = ? AND enabled = 1", (session.channel_id,)).fetchone():
            caption_service.remove(sid)
            raise HTTPException(404, "Kanál neexistuje.")
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > captions.MAX_AUDIO_BYTES:
                raise HTTPException(413, "Zvukový segment je příliš velký.")
            body.extend(chunk)
        try:
            job = caption_service.submit(sid, start, end, bytes(body))
        except KeyError:
            raise HTTPException(404, "Přepis už není aktivní.")
        except captions.CaptionBusy as error:
            raise HTTPException(429, str(error))
        except ValueError as error:
            raise HTTPException(422, str(error))

        async def stream_captions():
            async for event in caption_service.events(job):
                yield json.dumps(event, ensure_ascii=False) + "\n"
        return StreamingResponse(stream_captions(), media_type="application/x-ndjson",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    @app.patch("/api/channels/{channel_id}")
    async def api_patch(channel_id: int, body: PatchBody):
        row = con.execute("SELECT * FROM channels WHERE id = ?", (channel_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Kanál neexistuje.")
        sets, args = [], []
        if body.name is not None:
            if not body.name.strip():
                raise HTTPException(400, "Název nesmí být prázdný.")
            sets.append("name = ?"); args.append(body.name.strip())
        if body.group is not None:
            sets.append("grp = ?"); args.append(body.group.strip() or "Ostatní")
        if body.enabled is not None:
            sets.append("enabled = ?"); args.append(int(body.enabled))
            if body.enabled:   # po zapnutí dostane kanál novou šanci
                sets.append("fail_count = 0")
        if body.favorite is not None:
            sets.append("favorite = ?"); args.append(int(body.favorite))
        epg_changed = body.epg is not None and body.epg.strip() != row["epg"]
        if body.epg is not None:
            v = body.epg.strip()
            if v and ("#" not in v or not v.startswith(("http://", "https://"))):
                raise HTTPException(400, "Zdroj programu zadejte jako adresu souboru XMLTV, znak # a id kanálu.")
            sets.append("epg = ?"); args.append(v)
        if sets:
            con.execute(f"UPDATE channels SET {', '.join(sets)} WHERE id = ?", args + [channel_id])
            con.commit()
        if epg_changed:
            epg_soon(2)
        return _channel_json(con.execute("SELECT * FROM channels WHERE id = ?", (channel_id,)).fetchone())

    @app.post("/api/channels/order")
    async def api_order(body: OrderBody):
        """Uloží pořadí a skupiny podle toho, jak je uživatel přetáhl."""
        for i, it in enumerate(body.items, 1):
            con.execute("UPDATE channels SET sort = ?, grp = ? WHERE id = ?", (i, it.group.strip() or "Ostatní", it.id))
        con.commit()
        return {"saved": len(body.items)}

    @app.post("/api/groups/rename")
    async def api_group_rename(body: RenameBody):
        new = body.new.strip()
        if not new:
            raise HTTPException(400, "Název skupiny nesmí být prázdný.")
        n = con.execute("UPDATE channels SET grp = ? WHERE grp = ?", (new, body.old)).rowcount
        con.commit()
        return {"renamed": n}

    @app.get("/api/status")
    async def status():
        counts = {k: v for k, v in con.execute(
            "SELECT status, COUNT(*) FROM channels WHERE enabled = 1 GROUP BY status")}
        logrows = con.execute("SELECT ts, message FROM log ORDER BY id DESC LIMIT 30").fetchall()
        return {"last_check": db.get_meta(con, "last_check"), "running": checker._lock.locked(),
                "last_epg": db.get_meta(con, "last_epg"), "epg_running": epg._lock.locked(),
                "visible": len(db.visible(con, cfg.hide_after_failures)), "by_status": counts,
                "log": [dict(r) for r in logrows]}

    @app.post("/api/check", status_code=202)
    async def check():
        if checker._lock.locked():
            return JSONResponse({"started": False, "reason": "kontrola už běží"}, status_code=409)
        asyncio.create_task(checker.run_check(con, cfg))
        return {"started": True}

    @app.get("/epg.xml")
    async def epg_xml():
        body = epg.build_xmltv(con, db.visible(con, cfg.hide_after_failures))
        return Response(body, media_type="application/xml")

    @app.get("/epg.xml.gz")
    async def epg_xml_gz():
        body = epg.build_xmltv(con, db.visible(con, cfg.hide_after_failures))
        return Response(gzip.compress(body.encode("utf-8")), media_type="application/gzip")

    @app.get("/api/now")
    async def api_now():
        ids = [r["id"] for r in db.visible(con, cfg.hide_after_failures)]
        return {str(k): v for k, v in epg.now_next(con, ids).items()}

    @app.get("/api/schedule/{channel_id}")
    async def api_schedule(channel_id: int, limit: int = 12):
        return epg.schedule(con, channel_id, min(limit, 100))

    @app.post("/api/epg", status_code=202)
    async def api_epg():
        if epg._lock.locked():
            return JSONResponse({"started": False, "reason": "obnova programu už běží"}, status_code=409)
        asyncio.create_task(epg.refresh(con, cfg))
        return {"started": True}

    # --- katalog a přidávání kanálů ---

    @app.get("/api/catalog")
    async def api_catalog(q: str = "", country: str = "", category: str = "", language: str = "",
                          source: str = "", epg_only: bool = False, limit: int = 60, offset: int = 0,
                          restreams: bool = False, restreams_only: bool = False):
        return catalog.search(con, q, country, category, language, source, epg_only, min(limit, 200), offset,
                              restreams, restreams_only)

    @app.get("/api/catalog/facets")
    async def api_facets(q: str = "", country: str = "", category: str = "", language: str = "",
                         source: str = "", epg_only: bool = False, restreams: bool = False, restreams_only: bool = False):
        return catalog.facets(con, q, country, category, language, source, epg_only, restreams, restreams_only) | {
            "running": catalog._lock.locked()}

    @app.post("/api/catalog/refresh", status_code=202)
    async def api_catalog_refresh():
        if catalog._lock.locked():
            return JSONResponse({"started": False, "reason": "katalog se už obnovuje"}, status_code=409)
        asyncio.create_task(catalog.refresh(con, cfg))
        return {"started": True}

    @app.post("/api/catalog/import")
    async def api_catalog_import(body: UrlBody):
        try:
            return await catalog.import_playlist(con, cfg, body.url)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(400, f"Seznam se nepodařilo načíst: {e}")

    @app.post("/api/resolve")
    async def api_resolve(body: UrlsBody):
        return await catalog.resolve(con, body.urls)

    @app.post("/api/test")
    async def api_test(body: UrlBody):
        ok, detail = await catalog.test(con, body.url)
        return {"ok": ok, "detail": detail}

    @app.get("/api/groups")
    async def api_groups():
        return [r[0] for r in con.execute(
            "SELECT grp FROM channels WHERE grp != '' GROUP BY grp ORDER BY MIN(sort)")]

    @app.post("/api/channels")
    async def api_add(body: AddBody):
        if not body.url.startswith(("http://", "https://")):
            raise HTTPException(400, "Adresa musí začínat http:// nebo https://")
        if not body.skip_test:
            ok, detail = await catalog.test(con, body.url)
            if not ok:
                raise HTTPException(422, f"Stream teď nehraje ({detail}).")
        try:
            cid = catalog.add_channel(con, body.url, body.name.strip(), body.group.strip())
        except ValueError as e:
            raise HTTPException(400, str(e))
        epg_soon()
        return {"id": cid}

    @app.delete("/api/channels/{channel_id}")
    async def api_delete(channel_id: int):
        row = con.execute("SELECT name FROM channels WHERE id = ?", (channel_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Kanál neexistuje.")
        con.execute("DELETE FROM programmes WHERE channel_id = ?", (channel_id,))
        con.execute("DELETE FROM epg_status WHERE channel_id = ?", (channel_id,))
        con.execute("DELETE FROM channels WHERE id = ?", (channel_id,))
        db.log(con, f"Odebrán kanál {row['name']}")
        con.commit()
        return {"deleted": channel_id}

    @app.get("/api/proxy-link")
    async def api_proxy_link(url: str):
        """Odkaz přes proxy pro stream ze seznamu nebo katalogu."""
        if proxy.headers_for(con, url) is None:
            raise HTTPException(404, "Stream není v seznamu ani v katalogu.")
        return {"url": proxy.link(con, url)}

    @app.get("/live", include_in_schema=False)
    async def live(u: str):
        """Přesměruje na aktuální manifest živého vysílání z YouTube nebo Twitche."""
        if not webstream.is_web(u) or proxy.headers_for(con, u) is None:
            raise HTTPException(404, "Vysílání není v seznamu ani v katalogu.")
        try:
            return RedirectResponse(await webstream.resolve(u), status_code=302)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(502, f"Vysílání se nepodařilo načíst: {e}")

    @app.get("/proxy", include_in_schema=False)
    async def proxy_get(u: str, s: str = ""):
        try:
            url, signed = proxy.decode(con, u, s)
        except Exception:  # noqa: BLE001
            raise HTTPException(400, "Neplatný odkaz.")
        headers = proxy.headers_for(con, url)
        if headers is None and not signed:
            raise HTTPException(403, "Proxy přeposílá jen streamy z vašeho seznamu.")
        headers = headers or {"User-Agent": checker.DEFAULT_UA}
        try:
            if webstream.is_web(url):
                url = await webstream.resolve(url)
            r = await proxy.fetch(clients["proxy"], url, headers)
        except httpx.HTTPError as e:
            raise HTTPException(502, f"Zdroj neodpovídá: {type(e).__name__}")
        except RuntimeError as e:
            raise HTTPException(502, f"Vysílání se nepodařilo načíst: {e}")
        ctype = r.headers.get("content-type", "application/octet-stream")
        cors = {"Access-Control-Allow-Origin": "*", "Cache-Control": "no-cache"}
        # Playlist se načte celý a přepíše, segmenty se jen přeposílají po kouscích.
        first = b""
        it = r.aiter_bytes()
        try:
            first = await it.__anext__()
        except StopAsyncIteration:
            pass
        if r.status_code == 200 and proxy.is_playlist(ctype, first[:64]):
            body = first + b"".join([c async for c in it])
            await r.aclose()
            text = proxy.rewrite(con, body.decode("utf-8", errors="replace"), str(r.url))
            return Response(text, media_type="application/vnd.apple.mpegurl", headers=cors)

        async def stream():
            try:
                if first:
                    yield first
                async for chunk in it:
                    yield chunk
            finally:
                await r.aclose()
        return StreamingResponse(stream(), status_code=r.status_code, media_type=ctype, headers=cors)

    @app.get("/api/settings")
    async def api_settings():
        """Všechno technické pro stránku Nastavení na jednom místě."""
        lan = lan_addresses(cfg.port)
        base = lan.get("ip_url") or f"http://127.0.0.1:{cfg.port}"
        groups = [r[0] for r in con.execute(
            "SELECT grp FROM channels WHERE enabled = 1 GROUP BY grp ORDER BY MIN(sort)")]
        counts = {k: v for k, v in con.execute("SELECT status, COUNT(*) FROM channels WHERE enabled = 1 GROUP BY status")}
        backups = sorted((cfg.database.parent / "zalohy").glob("*.db"), reverse=True)
        wd_raw = db.get_meta(con, "wiki_wikidata")
        import json as _json
        wd = _json.loads(wd_raw) if wd_raw else {}
        return {
            "caption_usage": caption_usage.summary(),
            "addresses": lan,
            "playlists": [{"name": "Všechny kanály", "url": f"{base}/playlist.m3u"},
                          {"name": "Oblíbené", "url": f"{base}/playlist.m3u?favorites=true"}]
                         + [{"name": f"Skupina {g}", "url": f"{base}/playlist.m3u?group={quote(g)}"} for g in groups],
            "epg_url": f"{base}/epg.xml.gz",
            "channels": {"total": con.execute("SELECT COUNT(*) FROM channels").fetchone()[0],
                         "visible": len(db.visible(con, cfg.hide_after_failures)), "by_status": counts},
            "check": {"last": db.get_meta(con, "last_check"), "running": checker._lock.locked()},
            "epg": {"last": db.get_meta(con, "last_epg"), "running": epg._lock.locked(),
                    "with_epg": con.execute("SELECT COUNT(*) FROM epg_status WHERE count > 0").fetchone()[0]},
            "catalog": {"last": db.get_meta(con, "last_catalog"), "running": catalog._lock.locked(),
                        "total": con.execute("SELECT COUNT(*) FROM catalog").fetchone()[0],
                        "restreams": con.execute("SELECT COUNT(*) FROM catalog WHERE restream = 1").fetchone()[0],
                        "recommended": con.execute("SELECT COUNT(*) FROM catalog WHERE source = 'Doporučené'").fetchone()[0],
                        "blocked_hosts": cfg.blocked_hosts, "restream_hosts": cfg.restream_hosts},
            "wiki": {"last": db.get_meta(con, "last_wiki"), "running": _wiki_lock.locked(),
                     "dir": str(cfg.wiki_dir), "pages": len(wd),
                     "check": sum(1 for v in wd.values() if v.get("match") == "k ověření"),
                     "home": _obsidian(cfg, "Moje TV")},
            "backups": [{"name": b.name, "size": b.stat().st_size, "time": b.stat().st_mtime} for b in backups[:20]],
            "log": [dict(r) for r in con.execute("SELECT ts, message FROM log ORDER BY id DESC LIMIT 15")],
        }

    @app.post("/api/wiki", status_code=202)
    async def api_wiki():
        if _wiki_lock.locked():
            return JSONResponse({"started": False, "reason": "wiki se už generuje"}, status_code=409)
        asyncio.create_task(_wiki_job(con, cfg))
        return {"started": True}

    @app.post("/api/backup")
    async def api_backup():
        """Záloha databáze do data/zalohy. Obnova se dělá ručně, aby se nic nepřepsalo omylem."""
        import sqlite3 as _sqlite3
        from datetime import datetime as _dt
        d = cfg.database.parent / "zalohy"
        d.mkdir(parents=True, exist_ok=True)
        dest = d / f"tv.backup-{_dt.now().strftime('%Y-%m-%d-%H%M%S')}.db"
        target = _sqlite3.connect(dest)
        con.backup(target)
        target.close()
        db.log(con, f"Záloha {dest.name}")
        con.commit()
        return {"name": dest.name, "size": dest.stat().st_size}

    @app.get("/api/info")
    async def api_info():
        return lan_addresses(cfg.port)

    @app.get("/health", include_in_schema=False)
    async def health():
        return {"ok": True}

    return app
