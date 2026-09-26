import importlib.util
from pathlib import Path
import unittest

path = Path(__file__).resolve().parents[1] / 'integration' / 'injector.py'
spec = importlib.util.spec_from_file_location('integration_injector', path)
injector = importlib.util.module_from_spec(spec)
spec.loader.exec_module(injector)
e = injector.e

class InjectorBounds(unittest.TestCase):
    def test_keyboard_injection_is_rejected(self):
        with self.assertRaises(ValueError): injector.valid_events([[e.EV_KEY, e.KEY_A, 1]])
    def test_absolute_pointer_injection_is_rejected(self):
        with self.assertRaises(ValueError): injector.valid_events([[e.EV_ABS, e.ABS_X, 20]])
    def test_out_of_range_motion_is_rejected(self):
        with self.assertRaises(ValueError): injector.valid_events([[e.EV_REL, e.REL_X, 501]])
    def test_arbitrary_button_and_repeat_are_rejected(self):
        for event in ([e.EV_KEY, 999, 1], [e.EV_KEY, e.BTN_LEFT, 2]):
            with self.assertRaises(ValueError): injector.valid_events([event])
    def test_bool_fields_and_excess_batches_are_rejected(self):
        with self.assertRaises(ValueError): injector.valid_events([[True, e.BTN_LEFT, 1]])
        with self.assertRaises(ValueError): injector.valid_events([[e.EV_SYN, e.SYN_REPORT, 0]] * 129)
    def test_normal_middle_click_and_scroll_reports_are_allowed(self):
        events = [[e.EV_KEY, e.BTN_MIDDLE, 1], [e.EV_SYN, e.SYN_REPORT, 0],
                  [e.EV_KEY, e.BTN_MIDDLE, 0], [e.EV_SYN, e.SYN_REPORT, 0],
                  [e.EV_REL, e.REL_WHEEL, -1], [e.EV_SYN, e.SYN_REPORT, 0]]
        self.assertEqual(injector.valid_events(events), events)

if __name__ == '__main__': unittest.main()
