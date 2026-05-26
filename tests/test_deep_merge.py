from ceph_devstack import deep_merge


class TestDeepMerge:
    def test_deep_merge_empty_maps(self):
        result = deep_merge()
        assert result == {}

    def test_deep_merge_single_map(self):
        m = {"a": 1, "b": 2}
        result = deep_merge(m)
        assert result == m

    def test_deep_merge_two_maps_no_overlap(self):
        m1 = {"a": 1}
        m2 = {"b": 2}
        result = deep_merge(m1, m2)
        assert result == {"a": 1, "b": 2}

    def test_deep_merge_two_maps_with_overlap(self):
        m1 = {"a": 1, "b": 2}
        m2 = {"b": 3, "c": 4}
        result = deep_merge(m1, m2)
        assert result == {"a": 1, "b": 3, "c": 4}

    def test_deep_merge_nested_dicts(self):
        m1 = {"a": {"x": 1, "y": 2}}
        m2 = {"a": {"y": 3, "z": 4}}
        result = deep_merge(m1, m2)
        assert result == {"a": {"x": 1, "y": 3, "z": 4}}

    def test_deep_merge_three_maps(self):
        m1 = {"a": 1}
        m2 = {"b": 2}
        m3 = {"c": 3}
        result = deep_merge(m1, m2, m3)
        assert result == {"a": 1, "b": 2, "c": 3}

    def test_deep_merge_nested_override(self):
        m1 = {"outer": {"inner": "default", "keep": "value"}}
        m2 = {"outer": {"inner": "override"}}
        result = deep_merge(m1, m2)
        assert result["outer"]["inner"] == "override"
        assert result["outer"]["keep"] == "value"

    def test_deep_merge_with_none_value(self):
        m1 = {"a": 1}
        m2 = {"b": None}
        result = deep_merge(m1, m2)
        assert result == {"a": 1, "b": None}

    def test_deep_merge_with_list_values(self):
        m1 = {"a": [1, 2, 3]}
        m2 = {"a": [4, 5]}
        result = deep_merge(m1, m2)
        assert result["a"] == [4, 5]

    def test_deep_merge_does_not_modify_original_maps(self):
        m1 = {"a": {"x": 1}}
        m2 = {"a": {"y": 2}}
        m1_copy = {"a": {"x": 1}}
        m2_copy = {"a": {"y": 2}}
        deep_merge(m1, m2)
        assert m1 == m1_copy
        assert m2 == m2_copy

    def test_deep_merge_with_different_types(self):
        m1 = {"a": 1}
        m2 = {"a": "string"}
        result = deep_merge(m1, m2)
        assert result["a"] == "string"
