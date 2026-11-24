# Bezpečnost a soukromí

Moje TV je aplikace pro vlastní počítač a důvěryhodnou domácí síť. Nemá přihlášení,
oddělené uživatelské účty ani ochranu veřejně dostupné služby. Člověk, který dosáhne
na server, může spravovat kanály, nahrávky a spouštět nakonfigurované placené API.

- Výchozí adresa je `127.0.0.1`. `0.0.0.0` zapínejte jen pro důvěryhodnou LAN.
- Port neposílejte do veřejného Internetu. Pro vzdálený přístup použijte privátní síť
  s vlastní autentizací a omezením přístupu.
- Vkládejte jen vlastní či důvěryhodné playlisty. Server stahuje jejich URL a používá
  FFmpeg / yt-dlp; síťové zdroje nelze považovat za izolovaný sandbox.
- API klíče patří do prostředí nebo do ignorovaného souboru. `data/`, `.env` a
  `config.toml` nezveřejňujte; obsahují osobní data a mohou odkazovat na přihlášené zdroje.
- Lokální přepis neodesílá zvuk do cloudového ASR. Výslovně zapnutý placený překlad
  odesílá text poskytovateli. Chování externích služeb se řídí jejich podmínkami.
- Nahrávky a exportované texty jsou soukromé soubory uživatele, nikoli demonstrační
  obsah určený do Gitu.

Citlivý bezpečnostní problém hlaste soukromě přes GitHub Private Vulnerability
Reporting, pokud je pro repozitář zapnuté. Do veřejného issue nedávejte klíče,
neveřejné URL, databáze ani osobní přepisy.
