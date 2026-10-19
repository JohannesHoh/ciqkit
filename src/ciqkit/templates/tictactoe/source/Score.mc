import Toybox.Application;
import Toybox.Lang;

// Games won / drawn / lost, kept across launches in Application.Storage.
// (Storage, not Properties: Properties only holds keys declared in a
// settings resource and quietly ignores anything else.)
class Score {
    var won as Number = 0;
    var drawn as Number = 0;
    var lost as Number = 0;

    function initialize() {
        won = load("won");
        drawn = load("drawn");
        lost = load("lost");
    }

    // Count a finished game. `winner` is HUMAN, WATCH or EMPTY for a draw.
    function record(winner as Number) as Void {
        if (winner == Constants.HUMAN) {
            won++;
            Storage.setValue("won", won);
        } else if (winner == Constants.WATCH) {
            lost++;
            Storage.setValue("lost", lost);
        } else {
            drawn++;
            Storage.setValue("drawn", drawn);
        }
    }

    private function load(key as String) as Number {
        var v = Storage.getValue(key);
        return v instanceof Number ? v : 0;
    }
}
