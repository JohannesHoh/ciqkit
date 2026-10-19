import Toybox.Lang;
import Toybox.WatchUi;

// Input for every device: buttons, touch, or both.
//
//   START / ENTER      place a mark on the cursor cell (or start / play again)
//   UP / DOWN          move the cursor (swipes do the same on touch screens)
//   tap a cell         place a mark there
//   tap elsewhere      start / play again
//   BACK               to the home screen; from the home screen, exit
//
// onSelect() is deliberately NOT overridden. A BehaviorDelegate turns a screen
// tap into the "select" behaviour first and only calls onTap() if that returns
// false, so handling select here would throw away where the tap landed. The
// START / ENTER button still arrives, as a key, in onKey().
class GameDelegate extends WatchUi.BehaviorDelegate {
    private var _game as Game;
    private var _view as GameView;

    function initialize(game as Game, view as GameView) {
        BehaviorDelegate.initialize();
        _game = game;
        _view = view;
    }

    function onKey(keyEvent as WatchUi.KeyEvent) as Boolean {
        var key = keyEvent.getKey();
        if (key == WatchUi.KEY_ENTER || key == WatchUi.KEY_START) {
            _game.select();
            WatchUi.requestUpdate();
            return true;
        }
        return false;
    }

    function onTap(clickEvent as WatchUi.ClickEvent) as Boolean {
        if (_game.phase == Game.PHASE_PLAY) {
            var xy = clickEvent.getCoordinates();
            var cell = _view.cellAt(xy[0], xy[1]);
            if (cell < 0) {
                // A tap beside the grid is not a move. Swallow it rather than
                // guess which cell was meant.
                return true;
            }
            _game.play(cell);
        } else {
            _game.select();
        }
        WatchUi.requestUpdate();
        return true;
    }

    function onNextPage() as Boolean {
        _game.moveCursor(1);
        WatchUi.requestUpdate();
        return true;
    }

    function onPreviousPage() as Boolean {
        _game.moveCursor(-1);
        WatchUi.requestUpdate();
        return true;
    }

    function onBack() as Boolean {
        if (_game.back()) {
            WatchUi.requestUpdate();
            return true;
        }
        return false;   // home screen: let the system close the app
    }
}
