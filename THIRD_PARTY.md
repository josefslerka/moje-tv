# Použité komponenty

MIT licence tohoto repozitáře pokrývá vlastní kód aplikace. Knihovny, modely,
katalogy, kanálová loga a televizní obsah mají vlastní licence či podmínky.
Repozitář neobsahuje modelové váhy ani knihovny třetích stran jako vendored kopie.

| Komponenta | Použití | Licence / původ |
|---|---|---|
| [FastAPI](https://github.com/fastapi/fastapi) | Webové API | MIT |
| [Uvicorn](https://github.com/encode/uvicorn) | ASGI server | BSD-3-Clause |
| [HTTPX](https://github.com/encode/httpx) | Síťové požadavky | BSD-3-Clause |
| [markdown-it-py](https://github.com/executablebooks/markdown-it-py) | Vykreslení wiki | MIT |
| [yt-dlp](https://github.com/yt-dlp/yt-dlp) | Rozlišení živých webových streamů | Unlicense; závislosti mají vlastní licence |
| [HLS.js](https://github.com/video-dev/hls.js) | Přehrávání v prohlížeči | Apache-2.0; verze 1.5.13 se načítá z cdnjs |
| [SortableJS](https://github.com/SortableJS/Sortable) | Řazení kanálů | MIT; verze 1.15.2 se načítá z cdnjs |
| [MLX / MLX-LM](https://github.com/ml-explore/mlx-lm) | Volitelný lokální výpočet | MIT |
| [MLX Whisper](https://github.com/ml-explore/mlx-examples/tree/main/whisper) | Lokální přepis | MIT |
| [Whisper large-v3-turbo](https://huggingface.co/mlx-community/whisper-large-v3-turbo) | Model přepisu v MLX formátu | [Whisper MIT](https://github.com/openai/whisper/blob/main/LICENSE) a informace konkrétního checkpointu |
| [Hy-MT2](https://github.com/Tencent-Hunyuan/Hy-MT2) | Volitelný překlad | [Apache-2.0 upstream](https://github.com/Tencent-Hunyuan/Hy-MT2/blob/main/LICENSE.txt); [MLX checkpoint](https://huggingface.co/mlx-community/Hy-MT2-1.8B-8bit) |
| [FFmpeg](https://ffmpeg.org/legal.html) | Zvuk, video, nahrávání a AirPlay | Licence a volby konkrétní instalované sestavy |
| [iptv-org](https://github.com/iptv-org) | Volitelný katalog a metadata | Licence jednotlivých projektů; práva k obsahu drží jeho poskytovatel |
| [i.mjh.nz](https://i.mjh.nz/) | XMLTV program | Podmínky poskytovatele |
| [Wikidata](https://www.wikidata.org/wiki/Wikidata:Licensing) | Metadata ve wiki | CC0 pro strukturovaná data |
| [OpenRouter](https://openrouter.ai/) | Výslovně zapínaný placený překlad | Podmínky služby a vybraného modelu; klíč dodává uživatel |

Instalátor modelů ukládá přesnou revizi a verze knihoven do lokálního
`data/model-provenance.json`. Stažené licence a README modelů ponechává u vah.
Začlenění jiného modelu vyžaduje ověření jeho licence a kompatibility.
