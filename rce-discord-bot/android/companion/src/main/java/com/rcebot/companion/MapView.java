package com.rcebot.companion;

import android.content.Context;
import android.graphics.Canvas;
import android.graphics.Paint;
import android.graphics.Typeface;
import android.view.View;

import com.rcebot.ui.Ui;

import org.json.JSONArray;
import org.json.JSONObject;

/**
 * The team map: the world as a grid (cells of about 146 m, lettered across and numbered down like the in-game
 * map) with a marker for the player and each online team mate. No terrain – the game server doesn't hand it out.
 */
public class MapView extends View {
    private static final float CELL = 146.3f;
    private final Paint paint = new Paint(Paint.ANTI_ALIAS_FLAG);
    private JSONArray players = new JSONArray();
    private float size = 4000f;

    public MapView(Context c) {
        super(c);
    }

    void show(float worldSize, JSONArray who) {
        size = worldSize > 0 ? worldSize : 4000f;
        players = who == null ? new JSONArray() : who;
        invalidate();
    }

    static String letters(int column) {
        return column < 26 ? String.valueOf((char) ('A' + column))
                : "" + (char) ('A' + column / 26 - 1) + (char) ('A' + column % 26);
    }

    /** "K12" – the grid square of a position. */
    static String square(float worldSize, double x, double z) {
        int col = (int) Math.floor((x + worldSize / 2) / CELL), row = (int) Math.floor((worldSize / 2 - z) / CELL);
        int max = (int) Math.ceil(worldSize / CELL) - 1;
        return letters(Math.max(0, Math.min(max, col))) + Math.max(0, Math.min(max, row));
    }

    @Override
    protected void onMeasure(int w, int h) {
        int side = MeasureSpec.getSize(w);
        setMeasuredDimension(side, side);
    }

    @Override
    protected void onDraw(Canvas c) {
        float w = getWidth(), pad = Ui.dp(getContext(), 18), side = w - pad, scale = side / size;
        int cells = (int) Math.ceil(size / CELL);
        paint.setStyle(Paint.Style.FILL);
        paint.setColor(0xFF15140F);
        c.drawRect(pad, pad, w, w, paint);
        paint.setTypeface(Typeface.create("sans-serif-condensed", Typeface.BOLD));
        paint.setTextSize(Ui.dp(getContext(), 9));
        paint.setTextAlign(Paint.Align.CENTER);
        int step = cells > 20 ? 2 : 1;                       // labels on every second line on big maps
        for (int i = 0; i <= cells; i++) {
            float at = pad + Math.min(side, i * CELL * scale);
            paint.setColor(Ui.LINE);
            paint.setStrokeWidth(1f);
            c.drawLine(at, pad, at, w, paint);
            c.drawLine(pad, at, w, at, paint);
            if (i < cells && i % step == 0) {
                paint.setColor(Ui.MUTED);
                float mid = pad + (i + 0.5f) * CELL * scale;
                if (mid < w) {
                    c.drawText(letters(i), mid, pad - Ui.dp(getContext(), 5), paint);
                    c.drawText(String.valueOf(i), pad / 2, mid + Ui.dp(getContext(), 3), paint);
                }
            }
        }
        paint.setTextSize(Ui.dp(getContext(), 12));
        for (int i = 0; i < players.length(); i++) {
            JSONObject p = players.optJSONObject(i);
            float x = pad + (float) (p.optDouble("x") + size / 2) * scale;
            float y = pad + (float) (size / 2 - p.optDouble("z")) * scale;
            x = Math.max(pad + 6, Math.min(w - 6, x));
            y = Math.max(pad + 6, Math.min(w - 6, y));
            boolean me = p.optBoolean("me");
            float r = Ui.dp(getContext(), me ? 7 : 6);
            paint.setStyle(Paint.Style.FILL);
            paint.setColor(0xFF100F0D);
            c.drawCircle(x, y, r + Ui.dp(getContext(), 2), paint);
            paint.setColor(me ? Ui.ACCENT : Ui.OK);
            c.drawCircle(x, y, r, paint);
            paint.setColor(Ui.TEXT);
            paint.setTextAlign(x > w * 0.75f ? Paint.Align.RIGHT : Paint.Align.LEFT);
            float dx = (x > w * 0.75f ? -1 : 1) * (r + Ui.dp(getContext(), 5));
            paint.setShadowLayer(4f, 0f, 0f, 0xFF000000);
            c.drawText(p.optString("name"), x + dx, y + Ui.dp(getContext(), 4), paint);
            paint.clearShadowLayer();
            paint.setTextAlign(Paint.Align.CENTER);
        }
    }
}
