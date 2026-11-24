# Moje TV

Lokální IPTV přehrávač s živými AI titulky. Kanály spravujete v prohlížeči,
přehráváte je na počítači nebo přes M3U playlist v Apple TV. Rozhraní je v češtině.

A local IPTV player with live AI captions, Czech translation, recording, and
Apple TV playlist support. The interface and primary documentation are in Czech.

## Co umí

- Webový přehrávač HLS, katalog, vyhledávání, vlastní skupiny a řazení kanálů.
- Program XMLTV, kontrolu dostupnosti a hledání náhradních adres.
- Titulky vysílatele a volitelný lokální přepis Whisper s detekcí jazyka.
- Experimentální lokální překlad do češtiny přes Hy-MT2 1.8B, 8bit MLX.
- Výslovně zapínaný placený překlad přes OpenRouter včetně evidence nákladů.
- AirPlay s titulky a nahrávání videa i textu do lokálních souborů.
- Export M3U pro Apple TV a generování wiki s vlastními poznámkami.

Projekt neobsahuje hotový seznam vašich kanálů, televizní nahrávky, modely ani
API klíče. Přidejte vlastní zdroje nebo použijte volitelný veřejný katalog.
Používejte obsah, ke kterému máte oprávněný přístup; dostupnost se liší podle
vysílatele a regionu. Licence aplikace se nevztahuje na televizní obsah.

## Rychlý start

Potřebujete Python 3.11 nebo novější a [uv](https://docs.astral.sh/uv/).
Pro nahrávání, AirPlay a AI titulky je potřeba FFmpeg včetně `ffprobe`.

```sh
git clone https://github.com/josefslerka/moje-tv.git
cd moje-tv
uv sync --extra dev
cp config.example.toml config.toml
./run.sh
```

Otevřete **http://localhost:8090**. „Moje kanály“ jsou na začátku prázdné.
Na stránce **Přidat kanály** lze vložit URL streamu, importovat M3U nebo vybírat
z katalogu iptv-org, který se obnovuje na pozadí. Síťová chyba katalogu nebrání
spuštění přehrávače ani ručnímu přidání streamu.

Na macOS se FFmpeg instaluje například `brew install ffmpeg`. Pro YouTube/Twitch
je navíc vhodné `brew install deno`; jejich adresy při přehrávání řeší yt-dlp.
Základní server není závislý na MLX. Lokální AI vyžaduje Mac s Apple Silicon.

### Apple TV a domácí síť

V `config.toml` nastavte `host = "0.0.0.0"` a server restartujte. Mac a televize
musí být ve stejné důvěryhodné síti. Ve stránce **Nastavení** jsou aktuální adresy:

- playlist: `http://ADRESA-MACU:8090/playlist.m3u`
- program: `http://ADRESA-MACU:8090/epg.xml.gz`

V IPTV přehrávači na Apple TV přidejte M3U přes URL a zdroj programu XMLTV.
Skupiny a pořadí se přenášejí playlistem. AI titulky v běžném M3U exportu nejsou;
pro ně použijte Safari a tlačítko **AirPlay** ve webovém přehrávači.
Projekt zatím neobsahuje vlastní nativní tvOS aplikaci.

**Server nemá přihlášení.** Výchozí nastavení poslouchá jen na tomto počítači.
Nevystavujte port 8090 veřejně; více v [SECURITY.md](SECURITY.md).

## Lokální AI titulky na Apple Silicon

Instalace se provádí ve vlastní `.venv`; nesahá do systémového Pythonu:

```sh
uv sync --extra dev --extra ai
uv run python scripts/prepare_models.py
```

Skript stáhne veřejné MLX modely Whisper large-v3-turbo a Hy-MT2-1.8B-8bit do
`data/models/` a vytvoří `data/caption_runtime.json` s cestami této instalace.
Použité revize a verze knihoven uloží do `data/model-provenance.json`.
Počítejte s několika GB dat a s pamětí pro oba modely. Váhy nejsou součástí Gitu.
Modely mají [vlastní licence](THIRD_PARTY.md).

Pro samotný přepis použijte `--asr-only`. Existující lokální kopie lze předat
přes `--whisper-path` a `--translator-path`; `--no-download` zakáže síťové stahování.
Volba `--check` pouze ověří existující runtime. Nový runtime nepřepisuje bez `--force`.

Titulky jsou při otevření přehrávače vypnuté. Automatický režim dává přednost
stopě vysílatele, jinak použije lokální přepis. Lokální překlad je experimentální:
může změnit význam nebo něco vynechat a při pomalejším výpočtu dorazit pozdě.
Podpora detekce jazyka neznamená ověřenou kvalitu překladu každého jazyka.

### Volitelné placené API

`uv run python scripts/prepare_models.py --asr-only --enable-openrouter` připraví
Whisper a volitelnou sekci OpenRouter. Vlastní klíč nastavte do prostředí serveru
jako `OPENROUTER_API_KEY`; nikdy jej nezapisujte do sledovaného souboru.
Alternativou je `openrouter.env_file` v ignorovaném souboru runtime.

Placení se zapíná pouze výběrem **Placené API · Gemini → čeština**. Zvuk zůstává
lokálně, poskytovateli se posílá text titulků. V Nastavení se evidují známé náklady
vrácené API; neúplné požadavky mohou mít neznámou cenu. Dostupnost modelu a ceny
poskytovatele se mohou měnit. Bezplatné režimy na placený automaticky nepřepínají.

## Nahrávání a data

Tlačítko **Nahrát** ukládá soubory do `data/recordings/`. Dostupné režimy jsou video,
text nebo obojí. Uložený text může posloužit jako vstup pro ručně připravené shrnutí;
automatický generátor shrnutí není součástí aplikace.

Databáze, náklady na API, záznamy, logy, cache a generovaná wiki zůstávají pod
`data/`. Celá složka, místní konfigurace a `.env` soubory jsou ignorované Gitem.
Zálohujte je podle vlastní potřeby, nikoli do veřejného repozitáře.

## Příkazy a konfigurace

```sh
uv run tvserver catalog                  # obnova veřejného katalogu
uv run tvserver check --no-repair         # kontrola vlastních kanálů
uv run tvserver epg                      # obnova programu
uv run tvserver import /cesta/seznam.m3u  # přidání vlastního seznamu
uv run tvserver export data/playlist.m3u # export viditelných kanálů
uv run tvserver wiki --no-wikidata        # wiki bez nových dotazů na Wikidata
```

Cesty v `config.toml` jsou relativní k souboru konfigurace. Wiki používá `data/wiki`;
pro Obsidian vyplňte název vlastního trezoru a cestu. Ruční poznámky ve wiki se při
přegenerování zachovávají. Údaje o vlastnících z Wikidat mohou vyžadovat ruční ověření.

Pro automatické spouštění na Macu si vygenerujte vlastní launchd konfiguraci:

```sh
uv run python scripts/render_launch_agent.py --output data/io.github.moje-tv.plist
mkdir -p "$HOME/Library/LaunchAgents"
cp data/io.github.moje-tv.plist "$HOME/Library/LaunchAgents/"
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/io.github.moje-tv.plist"
```

Generátor používá skutečnou cestu dané instalace. Klíče do `.plist` nevkládá.
Automatické spouštění se vypne odpovídajícím `launchctl bootout`.

## Vývoj

```sh
uv sync --extra dev
uv run pytest -q
node --test tests/test_subtitles.cjs
uv build
```

Testy nevyžadují skutečné televizní vysílání, stažené AI modely ani placené API.
CI ověřuje Python 3.11/3.12, frontendové titulky a sestavení balíčku.
Pokyny k rozšíření jsou v [CONTRIBUTING.md](CONTRIBUTING.md).

## Licence

Kód aplikace je pod [MIT](LICENSE). Závislosti, modely, katalogy a poskytovatelé
obsahu mají vlastní licence a podmínky; přehled je v [THIRD_PARTY.md](THIRD_PARTY.md).
