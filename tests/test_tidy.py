from image2ppt.tidy import tidy

W, H = 1200, 600  # tolerance = 1.2 % of W = 14.4 px, grid = 5 px


def rect(i, x, y, w, h, **kw):
    return {"id": i, "type": "rect", "box": [x, y, w, h], "fill": "FFFFFF", "line": "000000", **kw}


def layout(*els):
    return {"image_size": [W, H], "background": "FFFFFF", "elements": list(els)}


def get(lay, i):
    return next(e for e in lay["elements"] if e["id"] == i)


def test_near_equal_edges_are_aligned():
    lay, _ = tidy(layout(rect("a", 100, 100, 200, 100), rect("b", 106, 300, 200, 100)))
    assert get(lay, "a")["box"][0] == get(lay, "b")["box"][0]


def test_distant_edges_stay_apart():
    lay, _ = tidy(layout(rect("a", 100, 100, 200, 100), rect("b", 160, 300, 200, 100)))
    assert get(lay, "a")["box"][0] != get(lay, "b")["box"][0]


def test_near_equal_sizes_are_normalised():
    lay, _ = tidy(layout(rect("a", 100, 100, 200, 100), rect("b", 100, 300, 209, 108)))
    assert get(lay, "a")["box"][2:] == get(lay, "b")["box"][2:]


def test_different_sizes_stay_different():
    lay, _ = tidy(layout(rect("a", 100, 100, 200, 100), rect("b", 100, 300, 300, 100)))
    assert get(lay, "a")["box"][2] != get(lay, "b")["box"][2]


def two_boxes_and_arrow(begin):
    return layout(rect("a", 100, 100, 200, 100), rect("b", 500, 100, 200, 100),
                  {"id": "l", "type": "arrow", "points": [list(begin), [498, 150]]})


def test_connector_end_snaps_to_edge_midpoint_within_threshold():
    lay, _ = tidy(two_boxes_and_arrow((310, 152)))  # 10 px right of the edge, 2 px off the midpoint
    line = get(lay, "l")
    assert line["anchors"]["begin"] == {"id": "a", "side": "right", "cxn": True}
    assert line["points"][0] == [300, 150]
    assert line["anchors"]["end"] == {"id": "b", "side": "left", "cxn": True}
    assert line["points"][1] == [500, 150]


def test_connector_end_stays_free_beyond_threshold():
    lay, _ = tidy(two_boxes_and_arrow((330, 152)))  # 30 px from the edge > 14.4 px
    line = get(lay, "l")
    assert "begin" not in line["anchors"]
    assert abs(line["points"][0][0] - 330) <= 5  # only grid-snapped


def test_connector_far_from_midpoint_sticks_to_edge_without_connection_point():
    lay, _ = tidy(layout(rect("a", 100, 100, 200, 100), rect("b", 500, 100, 200, 100),
                         {"id": "l", "type": "arrow", "points": [[302, 190], [498, 190]]}))
    begin = get(lay, "l")["anchors"]["begin"]
    assert begin["id"] == "a" and begin["cxn"] is False  # 40 px from the midpoint > 1.5 * tol


def test_split_word_is_rejoined():
    lay, _ = tidy(layout(rect("a", 100, 100, 200, 100, text="Lightweigh\nt\nAttention", line_px=22)))
    assert get(lay, "a")["text"] == "Lightweight\nAttention"


def test_font_size_is_set_for_text():
    lay, _ = tidy(layout(rect("a", 100, 100, 200, 100, text="Hello", line_px=22)))
    assert get(lay, "a")["size"] >= 6


def test_equal_gaps_in_a_row():
    lay, _ = tidy(layout(rect("a", 100, 100, 100, 100), rect("b", 300, 100, 100, 100),
                         rect("c", 520, 100, 100, 100)))
    xs = [get(lay, i)["box"][0] for i in "abc"]
    assert xs[1] - xs[0] == xs[2] - xs[1]
