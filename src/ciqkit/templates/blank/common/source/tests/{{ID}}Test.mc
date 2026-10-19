import Toybox.Lang;
import Toybox.Test;

// Unit tests. Built only by `monkeyc -t` and run by `ciqkit sim test`.
// The runner calls every (:test) function as a test case, so helpers that are
// not tests belong in a (:test) class.

(:test)
function testArithmeticStillWorks(logger as Logger) as Boolean {
    Test.assertEqual(1 + 1, 2);
    return true;
}
