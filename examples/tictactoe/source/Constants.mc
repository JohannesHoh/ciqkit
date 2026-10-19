import Toybox.Lang;

// Tunables for the game.
//
// `ciqkit sim shot --seed NAME=VALUE` (and the `seed` lists in ciqkit.toml)
// rewrite the value of one of these declarations for the length of a single
// job and then put this file back byte-for-byte. That only works while each
// tunable stays a one-line declaration of the form: const, name, "=", value,
// ";". Keep them that way.
module Constants {
    // Cell contents / players.
    const EMPTY = 0;
    const HUMAN = 1;
    const WATCH = 2;

    // Who moves first in a new game: HUMAN or WATCH.
    const FIRST_PLAYER = HUMAN;

    // How many moves ahead the watch looks (minimax plies).
    //   0  dumb and predictable: always takes the first free cell.
    //      Useful for screenshots and demos, because every game is the same.
    //   1  takes a winning move when it has one.
    //   2  also blocks yours.
    //   9  looks at every possible game; cannot be beaten.
    const AI_LEVEL = 2;

    // Cell the cursor starts on (0 = top left, 4 = centre, 8 = bottom right).
    const CURSOR_START = 4;
}
