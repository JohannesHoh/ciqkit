import Toybox.Lang;

// The watch's opponent: minimax (negamax form) with alpha-beta pruning,
// cut off after `level` plies.
module Ai {

    // Order in which candidate moves are tried: centre, corners, edges. Ties
    // go to the earliest, so the watch prefers the strong squares and plays
    // the same way every time for the same board (good for repeatable
    // screenshots). Trying strong moves first also makes pruning cut more.
    const ORDER = [4, 0, 2, 6, 8, 1, 3, 5, 7];

    const WIN = 100;

    // Index of the cell `player` should play, or -1 if the board is full.
    function chooseMove(cells as Array<Number>, player as Number, level as Number) as Number {
        if (level <= 0) {
            return Board.firstEmpty(cells);
        }
        // An empty board would mean searching every possible game. Fast in
        // the simulator, but on a watch a long callback trips the watchdog
        // and kills the app. The answer is known anyway: take the centre.
        if (Board.emptyCount(cells) == 9) {
            return 4;
        }
        var best = -1;
        var bestScore = -WIN - 1;
        for (var k = 0; k < ORDER.size(); k++) {
            var i = ORDER[k] as Number;
            if (cells[i] != Constants.EMPTY) {
                continue;
            }
            cells[i] = player;
            var score = -negamax(cells, Board.other(player), level - 1, 1, -WIN - 1, WIN + 1);
            cells[i] = Constants.EMPTY;
            if (score > bestScore) {
                bestScore = score;
                best = i;
            }
        }
        return best;
    }

    // Score of the position for `toMove`: positive is good for them. A win
    // found after fewer plies scores higher, so the watch wins as soon as it
    // can and, when losing, holds out as long as it can. Running out of
    // `depth` scores 0 ("no idea"), which is what makes low levels beatable.
    function negamax(cells as Array<Number>, toMove as Number, depth as Number,
                     ply as Number, alpha as Number, beta as Number) as Number {
        if (Board.winner(cells) != Constants.EMPTY) {
            // Only the player who just moved can have completed a line.
            return ply - WIN;
        }
        if (depth <= 0 || Board.isFull(cells)) {
            return 0;
        }
        var best = -WIN - 1;
        for (var k = 0; k < ORDER.size(); k++) {
            var i = ORDER[k] as Number;
            if (cells[i] != Constants.EMPTY) {
                continue;
            }
            cells[i] = toMove;
            var score = -negamax(cells, Board.other(toMove), depth - 1, ply + 1, -beta, -alpha);
            cells[i] = Constants.EMPTY;
            if (score > best) {
                best = score;
            }
            if (best > alpha) {
                alpha = best;
            }
            if (alpha >= beta) {
                break;
            }
        }
        return best;
    }
}
