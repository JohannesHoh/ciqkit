import Toybox.Lang;

// Pure game rules on a 3x3 board stored as an array of nine cells, row by row:
//
//   0 | 1 | 2
//   3 | 4 | 5
//   6 | 7 | 8
//
// No UI and no storage in here, so the unit tests in source/tests/ can
// call every function directly.
module Board {

    // The eight ways to get three in a row.
    const LINES = [
        [0, 1, 2], [3, 4, 5], [6, 7, 8],   // rows
        [0, 3, 6], [1, 4, 7], [2, 5, 8],   // columns
        [0, 4, 8], [2, 4, 6]               // diagonals
    ];

    function newCells() as Array<Number> {
        return [0, 0, 0, 0, 0, 0, 0, 0, 0];
    }

    // The line (three cell indices) that someone has completed, or null.
    function winningLine(cells as Array<Number>) as Array<Number>? {
        for (var i = 0; i < LINES.size(); i++) {
            var line = LINES[i] as Array<Number>;
            var a = cells[line[0]];
            if (a != Constants.EMPTY && a == cells[line[1]] && a == cells[line[2]]) {
                return line;
            }
        }
        return null;
    }

    // The player who has three in a row, or EMPTY when nobody has.
    function winner(cells as Array<Number>) as Number {
        var line = winningLine(cells);
        return line == null ? Constants.EMPTY : cells[line[0]];
    }

    function emptyCount(cells as Array<Number>) as Number {
        var n = 0;
        for (var i = 0; i < 9; i++) {
            if (cells[i] == Constants.EMPTY) {
                n++;
            }
        }
        return n;
    }

    function isFull(cells as Array<Number>) as Boolean {
        return emptyCount(cells) == 0;
    }

    // A full board with no winner.
    function isDraw(cells as Array<Number>) as Boolean {
        return isFull(cells) && winner(cells) == Constants.EMPTY;
    }

    // Lowest-numbered free cell, or -1 on a full board.
    function firstEmpty(cells as Array<Number>) as Number {
        for (var i = 0; i < 9; i++) {
            if (cells[i] == Constants.EMPTY) {
                return i;
            }
        }
        return -1;
    }

    function other(player as Number) as Number {
        return player == Constants.HUMAN ? Constants.WATCH : Constants.HUMAN;
    }
}
