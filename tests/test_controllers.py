import unittest

from pidlab.controllers import (
    CASCADE_BALL_SCHEMA,
    GROUP_INNER,
    LAYER_INNER,
    build_set_controller_message,
    default_parameters,
    specs_for_layer,
    validate_parameters,
)


class ControllerSchemaTests(unittest.TestCase):
    def test_defaults_cover_registered_parameters(self):
        defaults = default_parameters(CASCADE_BALL_SCHEMA)
        self.assertEqual(len(defaults), len(CASCADE_BALL_SCHEMA.parameters))
        self.assertIn("VELOCITY_KP_NORMAL", defaults)
        self.assertIn("POSITION_KP_FAR", defaults)

    def test_layer_returns_only_inner_parameters(self):
        specs = specs_for_layer(CASCADE_BALL_SCHEMA, LAYER_INNER)
        self.assertTrue(specs)
        self.assertTrue(all(spec.group == GROUP_INNER for spec in specs))

    def test_validation_rejects_unknown_and_out_of_range_values(self):
        with self.assertRaisesRegex(ValueError, "未知控制器参数"):
            validate_parameters(CASCADE_BALL_SCHEMA, {"UNKNOWN": 1.0})
        with self.assertRaisesRegex(ValueError, "超出范围"):
            validate_parameters(
                CASCADE_BALL_SCHEMA, {"VELOCITY_KP_NORMAL": 999.0}
            )

    def test_builds_generic_set_controller_message(self):
        message = build_set_controller_message(
            CASCADE_BALL_SCHEMA,
            LAYER_INNER,
            {"VELOCITY_KP_NORMAL": 0.04, "VELOCITY_KI_NORMAL": 0.0012},
        )
        self.assertEqual(message["type"], "set_controller")
        self.assertEqual(message["algorithm"], "cascade_ball_balance")
        self.assertEqual(message["layer"], "inner_velocity")
        self.assertEqual(message["params"]["VELOCITY_KP_NORMAL"], 0.04)


if __name__ == "__main__":
    unittest.main()
