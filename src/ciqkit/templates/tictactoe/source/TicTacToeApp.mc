import Toybox.Application;
import Toybox.Lang;
import Toybox.WatchUi;

class TicTacToeApp extends Application.AppBase {
    function initialize() {
        AppBase.initialize();
    }

    function getInitialView() as [WatchUi.Views] or [WatchUi.Views, WatchUi.InputDelegates] {
        var game = new Game();
        var view = new GameView(game);
        return [view, new GameDelegate(game, view)];
    }
}
