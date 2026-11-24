# Přispívání

Použijte Python 3.11+, `uv sync --extra dev`, `uv run pytest -q` a
`node --test tests/test_subtitles.cjs`. Změny přehrávače a titulků ověřte i v prohlížeči.

Přispívejte přes pull request s popisem změny a ověření. Testovací data mají být
syntetická. Nepřidávejte záznamy vysílání, osobní playlisty, placené přístupové URL,
klíče, váhy modelů nebo místní absolutní cesty. Veřejné katalogy a knihovny uvádějte
s jejich původem a licencí. AI přepis a překlad musí zůstat volitelné; placené API
se nesmí zapnout automaticky.

Server je rozdělen na `tvserver/` (API a služby), `tvserver/static/` (rozhraní),
`scripts/` (příprava instalace) a `tests/`. Soukromá provozní data patří pod `data/`.
