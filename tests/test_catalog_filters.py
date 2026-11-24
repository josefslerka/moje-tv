import itertools

import pytest
from fastapi.testclient import TestClient

from tvserver import catalog, db
from tvserver.app import create_app
from tvserver.config import Config


@pytest.fixture
def dataset(tmp_path):
    cfg = Config(database=tmp_path / "t.db", wiki_dir=tmp_path / "wiki")
    con = db.connect(cfg.database)

    def entry(path, tvg_id, name, country, categories, languages, source="Official", epg=1, restream=0):
        return dict(url=f"https://stream.test/{path}.m3u8", tvg_id=tvg_id, name=name, country=country,
                    categories=categories, languages=languages, source=source, has_epg=epg, restream=restream,
                    logo="", quality="", labels="", user_agent="", referrer="")

    rows = [entry("sport-hd", "Sport.de", "Sport DE", "DE", "sports,news", "deu,eng"),
            entry("sport-sd", "Sport.de", "Sport DE", "DE", "sports", "deu"),
            entry("sport-relay", "Sport.de", "Sport DE", "DE", "sports", "eng", "Suspect", 0, 1),
            entry("news", "News.de", "News DE", "DE", "news", "deu", epg=0),
            entry("sport-us", "Sport.us", "Sport US", "US", "sports", "eng", "Suspect", 1, 1),
            entry("no-id-us", "", "News without ID", "US", "news", "eng"),
            entry("no-id-de", "", "Italian Sport", "DE", "sports", "ita")]
    for source in ("Official", "Suspect"):
        catalog._store(con, source, [row for row in rows if row["source"] == source])
    con.commit()
    try:
        yield con, cfg
    finally:
        con.close()


def counts(facets, key):
    return {row["id"]: row["count"] for row in facets[key]}


def test_filter_counts_count_channels_instead_of_streams(dataset):
    con, _ = dataset
    f = catalog.facets(con)
    assert (f["total"], f["streams"], f["catalog_total"]) == (4, 5, 5)
    assert counts(f, "countries") == {"DE": 3, "US": 1}
    assert counts(f, "categories") == {"sports": 2, "news": 3}
    assert counts(f, "languages") == {"deu": 2, "eng": 2, "ita": 1}

    f = catalog.facets(con, restreams=True)
    assert (f["total"], f["streams"]) == (5, 7)
    assert counts(f, "countries") == {"DE": 3, "US": 2}
    assert counts(f, "sources") == {"Official": 4, "Suspect": 2}


def test_dropdown_counts_apply_other_selected_filters(dataset):
    con, _ = dataset
    f = catalog.facets(con, country="DE", category="sports", restreams=True)
    assert (f["total"], f["streams"]) == (2, 4)
    assert counts(f, "countries") == {"DE": 2, "US": 1}
    assert counts(f, "categories") == {"sports": 2, "news": 2}
    assert counts(f, "languages") == {"deu": 1, "eng": 1, "ita": 1}
    assert counts(f, "sources") == {"Official": 2, "Suspect": 1}


def test_empty_intersection_keeps_selected_zero_count_options(dataset):
    con, _ = dataset
    f = catalog.facets(con, country="US", category="sports", source="Official", restreams=True)
    assert f["total"] == 0
    assert counts(f, "countries") == {"DE": 2, "US": 0}
    assert counts(f, "categories") == {"news": 1, "sports": 0}
    assert counts(f, "sources") == {"Official": 0, "Suspect": 1}


def test_counts_match_results_for_filter_combinations(dataset):
    con, _ = dataset
    # Kontrola smlouvy mezi seznamem výsledků a počty v nabídkách, i při více zdrojích jednoho kanálu.
    for q, country, category, source, only_epg, mode in itertools.product(
            ("", "Sport"), ("", "DE"), ("", "sports"), ("", "Suspect"), (False, True), ("none", "all", "only")):
        filters = dict(q=q, country=country, category=category, source=source, epg_only=only_epg,
                       restreams=mode == "all", restreams_only=mode == "only")
        f = catalog.facets(con, **filters)
        assert f["total"] == catalog.search(con, **filters)["total"], filters
        for dimension, key in (("country", "countries"), ("category", "categories"), ("language", "languages"), ("source", "sources")):
            for value, count in counts(f, key).items():
                assert catalog.search(con, **(filters | {dimension: value}))["total"] == count, (filters, dimension, value)


def test_facets_api_accepts_the_same_filters_as_search(dataset):
    _, cfg = dataset
    filters = dict(q="Sport", country="US", category="sports", language="eng", source="Suspect",
                   epg_only="true", restreams_only="true")
    with TestClient(create_app(cfg, scheduler=False)) as client:
        f = client.get("/api/catalog/facets", params=filters).json()
        results = client.get("/api/catalog", params=filters).json()
    assert f["total"] == results["total"] == 1
    assert f["streams"] == 1
    assert counts(f, "countries") == {"US": 1}
    assert counts(f, "languages") == {"eng": 1}
