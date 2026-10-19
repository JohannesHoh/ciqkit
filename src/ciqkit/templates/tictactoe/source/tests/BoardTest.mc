import Toybox.Lang;
import Toybox.Test;

// Unit tests for the rules in Board.mc. Built only by `monkeyc -t` and run by
// `ciqkit sim test` (or `monkeydo app-test.prg DEVICE -t`).

// Builds a board from a nine-character picture, row by row: "X" (you), "O"
// (the watch) or "." per cell. A (:test) class rather than a (:test)
// function: the test runner calls every (:test) function as a test case, and
// this one is a helper.
(:test)
class Pic {
    static function cells(picture as String) as Array<Number> {
        var cells = Board.newCells();
        var chars = picture.toCharArray();
        for (var i = 0; i < 9; i++) {
            if (chars[i] == 'X') {
                cells[i] = Constants.HUMAN;
            } else if (chars[i] == 'O') {
                cells[i] = Constants.WATCH;
            }
        }
        return cells;
    }
}

(:test)
function testEmptyBoardHasNoWinner(logger as Logger) as Boolean {
    var cells = Board.newCells();
    Test.assertEqual(Board.winner(cells), Constants.EMPTY);
    Test.assert(!Board.isFull(cells));
    Test.assert(!Board.isDraw(cells));
    return true;
}

(:test)
function testEveryRowWins(logger as Logger) as Boolean {
    Test.assertEqual(Board.winner(Pic.cells("XXX.O.O..")), Constants.HUMAN);
    Test.assertEqual(Board.winner(Pic.cells("X.XOOOX..")), Constants.WATCH);
    Test.assertEqual(Board.winner(Pic.cells("O.O...XXX")), Constants.HUMAN);
    return true;
}

(:test)
function testEveryColumnWins(logger as Logger) as Boolean {
    Test.assertEqual(Board.winner(Pic.cells("XO.XO.X..")), Constants.HUMAN);
    Test.assertEqual(Board.winner(Pic.cells("XO.XO..O.")), Constants.WATCH);
    Test.assertEqual(Board.winner(Pic.cells("O.XO.X..X")), Constants.HUMAN);
    return true;
}

(:test)
function testBothDiagonalsWin(logger as Logger) as Boolean {
    Test.assertEqual(Board.winner(Pic.cells("XO..XO..X")), Constants.HUMAN);
    Test.assertEqual(Board.winner(Pic.cells("X.O.OXO.X")), Constants.WATCH);
    var line = Board.winningLine(Pic.cells("..X.X.X.O"));
    Test.assert(line != null);
    Test.assertEqual((line as Array<Number>)[0], 2);
    Test.assertEqual((line as Array<Number>)[2], 6);
    return true;
}

(:test)
function testTwoInARowIsNotAWin(logger as Logger) as Boolean {
    Test.assertEqual(Board.winner(Pic.cells("XX.OO....")), Constants.EMPTY);
    return true;
}

(:test)
function testFullBoardWithoutLineIsDraw(logger as Logger) as Boolean {
    var cells = Pic.cells("XOXXOOOXX");
    Test.assertEqual(Board.winner(cells), Constants.EMPTY);
    Test.assert(Board.isFull(cells));
    Test.assert(Board.isDraw(cells));
    Test.assertEqual(Board.firstEmpty(cells), -1);
    return true;
}

(:test)
function testWinOnLastMoveIsNotDraw(logger as Logger) as Boolean {
    var cells = Pic.cells("XOXOXOOXX");
    Test.assertEqual(Board.winner(cells), Constants.HUMAN);
    Test.assert(!Board.isDraw(cells));
    return true;
}
