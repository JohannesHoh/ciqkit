import Toybox.Lang;
import Toybox.Test;

// Unit tests for the watch's opponent in Ai.mc. The watch plays O here.

(:test)
function testAiTakesTheWin(logger as Logger) as Boolean {
    // O can complete the middle row at 5. X threatens the top row at 2, but
    // winning beats blocking.
    var cells = Pic.cells("XX.OO.X..");
    for (var level = 1; level <= 9; level++) {
        Test.assertEqual(Ai.chooseMove(cells, Constants.WATCH, level), 5);
    }
    return true;
}

(:test)
function testAiBlocksTheLoss(logger as Logger) as Boolean {
    // X threatens the middle column at 7; O has no win of its own.
    var cells = Pic.cells("OX..X....");
    for (var level = 2; level <= 9; level++) {
        Test.assertEqual(Ai.chooseMove(cells, Constants.WATCH, level), 7);
    }
    return true;
}

(:test)
function testLevelOneDoesNotLookAhead(logger as Logger) as Boolean {
    // Same board: one ply only sees its own move, so it does not block
    // (every move scores the same and the first in Ai.ORDER, 2, is taken).
    var cells = Pic.cells("OX..X....");
    Test.assertEqual(Ai.chooseMove(cells, Constants.WATCH, 1), 2);
    return true;
}

(:test)
function testLevelZeroTakesFirstFreeCell(logger as Logger) as Boolean {
    Test.assertEqual(Ai.chooseMove(Pic.cells("X...O...."), Constants.WATCH, 0), 1);
    return true;
}

(:test)
function testAiLeavesBoardUnchanged(logger as Logger) as Boolean {
    var cells = Pic.cells("X...O...X");
    Ai.chooseMove(cells, Constants.WATCH, 9);
    Test.assertEqual(Board.emptyCount(cells), 6);
    Test.assertEqual(cells[0], Constants.HUMAN);
    Test.assertEqual(cells[8], Constants.HUMAN);
    return true;
}

(:test)
function testPerfectAiNeverLosesToItself(logger as Logger) as Boolean {
    // Two perfect players always draw.
    var cells = Board.newCells();
    var player = Constants.HUMAN;
    while (Board.winner(cells) == Constants.EMPTY && !Board.isFull(cells)) {
        cells[Ai.chooseMove(cells, player, 9)] = player;
        player = Board.other(player);
    }
    Test.assert(Board.isDraw(cells));
    return true;
}
