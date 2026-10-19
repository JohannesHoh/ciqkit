import Toybox.Graphics;
import Toybox.Lang;
import Toybox.System;
import Toybox.WatchUi;

// Draws whichever screen the game is on: home, the board, or the result.
//
// All positions are fractions of the screen size, so one layout serves the
// 260 px MIP round, the 454 px AMOLED round and the 320x360 rectangle.
class GameView extends WatchUi.View {
    const COLOR_X = 0x00AAFF;
    const COLOR_O = 0xFF5500;
    const COLOR_CURSOR = 0xFFFF00;
    const COLOR_BUTTON = 0x00AA55;

    private var _game as Game;
    private var _w as Number = 0;
    private var _h as Number = 0;
    // The play grid: top-left corner and side length, in pixels.
    private var _gx as Number = 0;
    private var _gy as Number = 0;
    private var _gs as Number = 0;

    private var _title as String;
    private var _start as String;
    private var _won as String;
    private var _drawn as String;
    private var _lost as String;
    private var _youWin as String;
    private var _watchWins as String;
    private var _draw as String;
    private var _again as String;

    function initialize(game as Game) {
        View.initialize();
        _game = game;
        // Load every string once, not on each redraw.
        _title = WatchUi.loadResource(Rez.Strings.Title) as String;
        _start = WatchUi.loadResource(Rez.Strings.Start) as String;
        _won = WatchUi.loadResource(Rez.Strings.Won) as String;
        _drawn = WatchUi.loadResource(Rez.Strings.Drawn) as String;
        _lost = WatchUi.loadResource(Rez.Strings.Lost) as String;
        _youWin = WatchUi.loadResource(Rez.Strings.YouWin) as String;
        _watchWins = WatchUi.loadResource(Rez.Strings.WatchWins) as String;
        _draw = WatchUi.loadResource(Rez.Strings.Draw) as String;
        _again = WatchUi.loadResource(Rez.Strings.Again) as String;
    }

    function onLayout(dc as Dc) as Void {
        _w = dc.getWidth();
        _h = dc.getHeight();
        var round = System.getDeviceSettings().screenShape == System.SCREEN_SHAPE_ROUND;
        // On a round face the grid has to fit inside the circle: the largest
        // square that does is 70.7% of the diameter, and 66% leaves a margin
        // so the corner cells are not shaved by the bezel.
        _gs = round ? _w * 66 / 100 : (_w < _h ? _w : _h) * 86 / 100;
        _gs -= _gs % 3;
        _gx = (_w - _gs) / 2;
        _gy = (_h - _gs) / 2;
    }

    function onUpdate(dc as Dc) as Void {
        dc.setColor(Graphics.COLOR_WHITE, Graphics.COLOR_BLACK);
        dc.clear();
        if (dc has :setAntiAlias) {
            dc.setAntiAlias(true);
        }
        if (_game.phase == Game.PHASE_HOME) {
            drawHome(dc);
        } else if (_game.phase == Game.PHASE_PLAY) {
            drawBoard(dc, _gx, _gy, _gs, true);
        } else {
            drawResult(dc);
        }
    }

    // Grid cell under screen point (x, y), or -1 outside the grid.
    function cellAt(x as Number, y as Number) as Number {
        if (_gs <= 0 || x < _gx || y < _gy || x >= _gx + _gs || y >= _gy + _gs) {
            return -1;
        }
        var cell = _gs / 3;
        return ((y - _gy) / cell) * 3 + (x - _gx) / cell;
    }

    private function drawHome(dc as Dc) as Void {
        var cx = _w / 2;
        dc.setColor(Graphics.COLOR_WHITE, Graphics.COLOR_TRANSPARENT);
        dc.drawText(cx, _h * 24 / 100, Graphics.FONT_MEDIUM, _title, centred());

        var s = _game.score;
        drawCount(dc, _w * 30 / 100, _won, s.won, COLOR_X);
        drawCount(dc, cx, _drawn, s.drawn, Graphics.COLOR_LT_GRAY);
        drawCount(dc, _w * 70 / 100, _lost, s.lost, COLOR_O);

        drawButton(dc, _h * 75 / 100, _start, Graphics.FONT_SMALL);
    }

    private function drawCount(dc as Dc, x as Number, label as String, n as Number,
                               color as Number) as Void {
        dc.setColor(Graphics.COLOR_LT_GRAY, Graphics.COLOR_TRANSPARENT);
        dc.drawText(x, _h * 42 / 100, Graphics.FONT_XTINY, label, centred());
        dc.setColor(color, Graphics.COLOR_TRANSPARENT);
        dc.drawText(x, _h * 54 / 100, Graphics.FONT_MEDIUM, n.toString(), centred());
    }

    private function drawResult(dc as Dc) as Void {
        var cx = _w / 2;
        var text = _draw;
        var color = Graphics.COLOR_WHITE;
        if (_game.result == Constants.HUMAN) {
            text = _youWin;
            color = COLOR_X;
        } else if (_game.result == Constants.WATCH) {
            text = _watchWins;
            color = COLOR_O;
        }
        dc.setColor(color, Graphics.COLOR_TRANSPARENT);
        dc.drawText(cx, _h * 20 / 100, Graphics.FONT_MEDIUM, text, centred());

        // The final position, small, so you can see how it ended.
        var size = (_w < _h ? _w : _h) * 30 / 100;
        size -= size % 3;
        drawBoard(dc, (_w - size) / 2, _h * 46 / 100 - size / 2, size, false);

        var s = _game.score;
        dc.setColor(Graphics.COLOR_LT_GRAY, Graphics.COLOR_TRANSPARENT);
        dc.drawText(cx, _h * 69 / 100, Graphics.FONT_XTINY,
                    _won + " " + s.won + "   " + _drawn + " " + s.drawn + "   "
                    + _lost + " " + s.lost, centred());

        drawButton(dc, _h * 82 / 100, _again, Graphics.FONT_TINY);
    }

    // A filled pill with a label, centred horizontally at height y. It is a
    // hint, not a hit target: on the home and result screens a tap anywhere
    // (or START) continues.
    private function drawButton(dc as Dc, y as Number, label as String,
                                font as Graphics.FontType) as Void {
        var th = dc.getFontHeight(font);
        var bw = dc.getTextWidthInPixels(label, font) + th * 2;
        var bh = th + th / 3;
        dc.setColor(COLOR_BUTTON, Graphics.COLOR_TRANSPARENT);
        dc.fillRoundedRectangle((_w - bw) / 2, y - bh / 2, bw, bh, bh / 2);
        dc.setColor(Graphics.COLOR_BLACK, Graphics.COLOR_TRANSPARENT);
        dc.drawText(_w / 2, y, font, label, centred());
    }

    // The 3x3 board with its marks at (x, y), `size` pixels square.
    private function drawBoard(dc as Dc, x as Number, y as Number, size as Number,
                               withCursor as Boolean) as Void {
        var cell = size / 3;
        var cells = _game.cells;

        if (withCursor) {
            var c = _game.cursor;
            var inset = cell / 14 + 2;
            dc.setColor(COLOR_CURSOR, Graphics.COLOR_TRANSPARENT);
            dc.setPenWidth(cell / 22 + 2);
            dc.drawRoundedRectangle(x + (c % 3) * cell + inset, y + (c / 3) * cell + inset,
                                    cell - 2 * inset, cell - 2 * inset, cell / 8);
        }

        dc.setColor(Graphics.COLOR_LT_GRAY, Graphics.COLOR_TRANSPARENT);
        dc.setPenWidth(size / 80 + 1);
        for (var k = 1; k < 3; k++) {
            dc.drawLine(x + k * cell, y, x + k * cell, y + size);
            dc.drawLine(x, y + k * cell, x + size, y + k * cell);
        }

        var pad = cell * 26 / 100;
        dc.setPenWidth(cell / 11 + 1);
        for (var i = 0; i < 9; i++) {
            var left = x + (i % 3) * cell;
            var top = y + (i / 3) * cell;
            if (cells[i] == Constants.HUMAN) {
                dc.setColor(COLOR_X, Graphics.COLOR_TRANSPARENT);
                dc.drawLine(left + pad, top + pad, left + cell - pad, top + cell - pad);
                dc.drawLine(left + cell - pad, top + pad, left + pad, top + cell - pad);
            } else if (cells[i] == Constants.WATCH) {
                dc.setColor(COLOR_O, Graphics.COLOR_TRANSPARENT);
                dc.drawCircle(left + cell / 2, top + cell / 2, cell / 2 - pad);
            }
        }

        var line = Board.winningLine(cells);
        if (line != null) {
            var a = line[0];
            var b = line[2];
            dc.setColor(Graphics.COLOR_WHITE, Graphics.COLOR_TRANSPARENT);
            dc.setPenWidth(cell / 14 + 1);
            dc.drawLine(x + (a % 3) * cell + cell / 2, y + (a / 3) * cell + cell / 2,
                        x + (b % 3) * cell + cell / 2, y + (b / 3) * cell + cell / 2);
        }
        dc.setPenWidth(1);
    }

    private function centred() as Number {
        return Graphics.TEXT_JUSTIFY_CENTER | Graphics.TEXT_JUSTIFY_VCENTER;
    }
}
