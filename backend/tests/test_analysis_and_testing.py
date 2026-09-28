from app import analysis, testing_engine, feedback_engine


# ----- test folder detection (Node/Jest projects use test/ or __tests__/, not only tests/)

def test_node_project_with_test_folder_gets_credit(tmp_path):
    (tmp_path / "test").mkdir()
    (tmp_path / "package.json").write_text("{}")
    result = analysis.check_structure(tmp_path, "node")
    assert "tests" in result["found"] and "tests" not in result["missing"]
    assert analysis.find_test_target(tmp_path) == "test"


def test_dunder_tests_folder_and_loose_test_files(tmp_path):
    (tmp_path / "__tests__").mkdir()
    assert analysis.find_test_dir(tmp_path) == "__tests__"

    loose = tmp_path / "loose"
    loose.mkdir()
    (loose / "app.test.js").write_text("1")
    assert analysis.find_test_dir(loose) is None
    assert analysis.find_test_target(loose) == "."


def test_no_tests_at_all(tmp_path):
    (tmp_path / "main.py").write_text("print(1)")
    assert analysis.find_test_target(tmp_path) is None


# ----- listening-port discovery

PROC_NET_TCP = """  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode
   0: 00000000:1F40 00000000:0000 0A 00000000:00000000 00:00000000 00000000     0        0 12345 1
   1: 0100007F:1388 00000000:0000 0A 00000000:00000000 00:00000000 00000000     0        0 12346 1
   2: 0F00000A:D2A0 0100000A:0050 01 00000000:00000000 00:00000000 00000000     0        0 12347 1
"""


def test_parse_listening_sockets():
    sockets = testing_engine._parse_listening_sockets(PROC_NET_TCP)
    assert (8000, True) in sockets       # 0.0.0.0:8000 - reachable
    assert (5000, False) in sockets      # 127.0.0.1:5000 - loopback only
    assert len(sockets) == 2             # the ESTABLISHED (01) row is ignored


def test_candidate_ports_prefer_what_is_really_listening():
    assert testing_engine._candidate_ports([(3001, True)], exposed=[8080]) == [3001]
    fallback = testing_engine._candidate_ports([], exposed=[3001])
    assert fallback[0] == 3001 and 8000 in fallback and 5000 in fallback


# ----- maturity: a single-component average must not look like a real score

def test_engineering_maturity_partial_flag():
    image_only = feedback_engine.compute_engineering_maturity({"feature_completion": {"score": 100}})
    assert image_only["score"] == 100 and image_only["partial"] is True

    full = {k: {"score": 60} for k in feedback_engine.LABELS}
    assert feedback_engine.compute_engineering_maturity(full)["partial"] is False


def test_scoring_version_is_stamped():
    from app.sandbox_engine import _finalize_scores
    assert _finalize_scores({"feature_completion": {"score": 50}})["scoring_version"] == feedback_engine.SCORING_VERSION
