import unittest
from railsim_mcp.uc1_modes import UC1Mode, rounded_counts


def plan():
    return dict(
        modes=[
            dict(mix="MAINLINE", patterns=["FV_PASS", "FV_STOP"]),
            dict(mix="TRANSIT", patterns=["FV_STOP"]),
            dict(mix="EXPRESS", patterns=["FV_STOP"]),
            dict(mix="METRO", patterns=["RV_STOP"]),
        ],
        mixes={
            "MAINLINE": dict(shares=dict(FV=0.4, RV=0.4, GV=0.2)),
            "TRANSIT": dict(shares=dict(FV=0.5, GV=0.5)),
            "EXPRESS": dict(shares=dict(FV=1)),
            "METRO": dict(shares=dict(RV=1)),
        },
        patterns={
            "FV_PASS": dict(
                shares={"FV": {"LR": 1}, "RV": {"LMR": 1}, "GV": {"LR": 1}}
            ),
            "FV_STOP": dict(
                shares={"FV": {"LMR": 1}, "RV": {"LMR": 1}, "GV": {"LR": 1}}
            ),
            "RV_STOP": dict(shares={"RV": {"LMR": 1}}),
        },
        flows={
            "LR": dict(
                routes={"FV": dict(forward="FV_LR"), "GV": dict(forward="GV_LR")}
            ),
            "LMR": dict(
                routes={"FV": dict(forward="FV_LMR"), "RV": dict(forward="RV_LMR")}
            ),
        },
    )


class ModeTests(unittest.TestCase):
    def test_only_the_five_declared_modes_are_selected(self):
        data = plan()
        modes = [
            UC1Mode.from_plan(data, m["mix"] + "/" + p)
            for m in data["modes"]
            for p in m["patterns"]
        ]
        self.assertEqual(len(modes), 5)
        self.assertEqual(modes[1].routes["FV"], "FV_LMR")
        self.assertEqual(modes[4].routes, {"RV": "RV_LMR"})
        with self.assertRaises(ValueError):
            UC1Mode.from_plan(data, "EXPRESS/FV_PASS")

    def test_rounding_is_deterministic_and_has_no_absent_products(self):
        self.assertEqual(rounded_counts(5, dict(FV=0.5, GV=0.5)), dict(FV=3, GV=2))
        self.assertEqual(rounded_counts(13, dict(RV=1)), dict(RV=13))
        for n in range(4, 101):
            self.assertEqual(
                sum(rounded_counts(n, dict(FV=0.4, RV=0.4, GV=0.2)).values()), n
            )
        with self.assertRaises(ValueError):
            rounded_counts(5, dict(FV=0.6, RV=0.3))
