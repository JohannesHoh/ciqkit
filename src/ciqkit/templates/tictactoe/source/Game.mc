import Toybox.Lang;

// The game as a small state machine. Input handlers call into this; the view
// only reads it. Nothing in here draws or knows about buttons.
class Game {
    enum Phase {
        PHASE_HOME,
        PHASE_PLAY,
        PHASE_OVER
    }

    var phase as Phase = PHASE_HOME;
    var cells as Array<Number>;
    var cursor as Number = Constants.CURSOR_START;
    // Who won the last game: HUMAN, WATCH, or EMPTY for a draw.
    var result as Number = Constants.EMPTY;
    var score as Score;

    function initialize() {
        cells = Board.newCells();
        score = new Score();
    }

    function start() as Void {
        cells = Board.newCells();
        cursor = Constants.CURSOR_START;
        result = Constants.EMPTY;
        phase = PHASE_PLAY;
        if (Constants.FIRST_PLAYER == Constants.WATCH) {
            watchMove();
        }
    }

    // START / ENTER, or a tap outside the grid.
    function select() as Void {
        if (phase == PHASE_PLAY) {
            play(cursor);
        } else {
            start();
        }
    }

    // Put the player's mark on cell `i`, then let the watch answer.
    // Returns false (and changes nothing) if the cell is taken.
    function play(i as Number) as Boolean {
        if (phase != PHASE_PLAY || i < 0 || i > 8 || cells[i] != Constants.EMPTY) {
            return false;
        }
        cells[i] = Constants.HUMAN;
        cursor = i;
        if (!finishIfOver()) {
            watchMove();
        }
        return true;
    }

    // UP / DOWN (or a swipe): move the cursor to the next free cell.
    function moveCursor(step as Number) as Void {
        if (phase != PHASE_PLAY) {
            return;
        }
        var c = cursor;
        for (var n = 0; n < 9; n++) {
            c = (c + step + 9) % 9;
            if (cells[c] == Constants.EMPTY) {
                cursor = c;
                return;
            }
        }
    }

    // BACK. Returns false on the home screen so the system closes the app.
    function back() as Boolean {
        if (phase == PHASE_HOME) {
            return false;
        }
        // Leaving a game half way counts as nothing, not as a loss.
        phase = PHASE_HOME;
        return true;
    }

    private function watchMove() as Void {
        var m = Ai.chooseMove(cells, Constants.WATCH, Constants.AI_LEVEL);
        if (m >= 0) {
            cells[m] = Constants.WATCH;
        }
        if (!finishIfOver() && cells[cursor] != Constants.EMPTY) {
            // Keep the cursor on a cell the player can actually take.
            cursor = Board.firstEmpty(cells);
        }
    }

    private function finishIfOver() as Boolean {
        var w = Board.winner(cells);
        if (w == Constants.EMPTY && !Board.isFull(cells)) {
            return false;
        }
        result = w;
        phase = PHASE_OVER;
        score.record(w);
        return true;
    }
}
