import Toybox.Graphics;
import Toybox.Lang;
import Toybox.System;
import Toybox.WatchUi;

class {{ID}}View extends WatchUi.WatchFace {
    function initialize() {
        WatchFace.initialize();
    }

    function onUpdate(dc as Dc) as Void {
        var clock = System.getClockTime();
        var time = Lang.format("$1$:$2$", [clock.hour.format("%02d"), clock.min.format("%02d")]);
        var cx = dc.getWidth() / 2;
        var cy = dc.getHeight() / 2;
        dc.setColor(Graphics.COLOR_WHITE, Graphics.COLOR_BLACK);
        dc.clear();
        dc.drawText(cx, cy, Graphics.FONT_NUMBER_HOT, time,
                    Graphics.TEXT_JUSTIFY_CENTER | Graphics.TEXT_JUSTIFY_VCENTER);
    }

    // Sleep hooks: called when the watch enters or leaves low-power mode.
    function onExitSleep() as Void {
    }

    function onEnterSleep() as Void {
    }
}
