import Toybox.Lang;
import Toybox.WatchUi;

// Input goes here. BehaviorDelegate maps taps and buttons to onSelect, onBack,
// onNextPage... Override what you need; BACK exits the app by default.
class {{ID}}Delegate extends WatchUi.BehaviorDelegate {
    function initialize() {
        BehaviorDelegate.initialize();
    }
}
