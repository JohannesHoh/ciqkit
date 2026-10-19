import Toybox.Activity;
import Toybox.Graphics;
import Toybox.Lang;
import Toybox.WatchUi;

// A data field is computed once a second (compute) and drawn by onUpdate.
// For a single text value, extend WatchUi.SimpleDataField instead.
class {{ID}}View extends WatchUi.DataField {
    private var _value as Numeric = 0;

    function initialize() {
        DataField.initialize();
    }

    function compute(info as Activity.Info) as Void {
        var hr = info.currentHeartRate;
        _value = (hr != null) ? hr : 0;
    }

    function onUpdate(dc as Dc) as Void {
        dc.setColor(Graphics.COLOR_WHITE, Graphics.COLOR_BLACK);
        dc.clear();
        dc.drawText(dc.getWidth() / 2, dc.getHeight() / 2, Graphics.FONT_MEDIUM,
                    _value.format("%d"),
                    Graphics.TEXT_JUSTIFY_CENTER | Graphics.TEXT_JUSTIFY_VCENTER);
    }
}
