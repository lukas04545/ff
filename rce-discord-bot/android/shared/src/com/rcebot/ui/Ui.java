package com.rcebot.ui;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.Context;
import android.content.res.ColorStateList;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.graphics.drawable.RippleDrawable;
import android.text.InputType;
import android.util.TypedValue;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;

import androidx.swiperefreshlayout.widget.SwipeRefreshLayout;

/** The look of both apps, built in code: dark screens, rounded cards, pills, gradient buttons, stat tiles. */
public final class Ui {
    public static final int BG = 0xFF0B0E1A, CARD = 0xFF151A2E, CARD2 = 0xFF1D2442, LINE = 0xFF283055,
            TEXT = 0xFFE8ECF8, MUTED = 0xFF8F97B4, ACCENT = 0xFFFF5A36, ACCENT2 = 0xFFFF9A3C, OK = 0xFF3DDC84,
            WARN = 0xFFF5C542, BAD = 0xFFFF5C5C, BLUE = 0xFF5B8CFF;
    public static final int PRIMARY = 0, GHOST = 1, DANGER = 2;

    private Ui() {
    }

    public static int dp(Context c, float v) {
        return Math.round(TypedValue.applyDimension(TypedValue.COMPLEX_UNIT_DIP, v, c.getResources().getDisplayMetrics()));
    }

    public static GradientDrawable round(Context c, int color, float radiusDp) {
        GradientDrawable d = new GradientDrawable();
        d.setColor(color);
        d.setCornerRadius(dp(c, radiusDp));
        return d;
    }

    public static LinearLayout.LayoutParams lp(Context c, int w, int h, float l, float t, float r, float b) {
        LinearLayout.LayoutParams p = new LinearLayout.LayoutParams(w, h);
        p.setMargins(dp(c, l), dp(c, t), dp(c, r), dp(c, b));
        return p;
    }

    public static TextView text(Context c, CharSequence t, float sp, int color, boolean bold) {
        TextView v = new TextView(c);
        v.setText(t);
        v.setTextSize(sp);
        v.setTextColor(color);
        if (bold) v.setTypeface(Typeface.DEFAULT_BOLD);
        return v;
    }

    /** A small section heading inside a card ("KITS"). */
    public static TextView label(Context c, CharSequence t) {
        TextView v = text(c, t.toString().toUpperCase(), 11, MUTED, true);
        v.setLetterSpacing(0.12f);
        v.setLayoutParams(lp(c, ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT, 0, 16, 0, 4));
        return v;
    }

    public static LinearLayout card(Context c) {
        LinearLayout v = new LinearLayout(c);
        v.setOrientation(LinearLayout.VERTICAL);
        GradientDrawable bg = round(c, CARD, 18);
        bg.setStroke(dp(c, 1), LINE);
        v.setBackground(bg);
        int p = dp(c, 16);
        v.setPadding(p, p, p, p);
        v.setLayoutParams(lp(c, ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT, 0, 0, 0, 14));
        return v;
    }

    public static LinearLayout row(Context c) {
        LinearLayout v = new LinearLayout(c);
        v.setOrientation(LinearLayout.HORIZONTAL);
        v.setGravity(Gravity.CENTER_VERTICAL);
        v.setLayoutParams(lp(c, ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT, 0, 6, 0, 6));
        return v;
    }

    public static LinearLayout column(Context c) {
        LinearLayout v = new LinearLayout(c);
        v.setOrientation(LinearLayout.VERTICAL);
        return v;
    }

    /** Takes the free width of a row. */
    public static <T extends View> T grow(T v) {
        v.setLayoutParams(new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1));
        return v;
    }

    public static TextView pill(Context c, CharSequence t, int color) {
        TextView v = text(c, t, 12, color, true);
        v.setBackground(round(c, (color & 0x00FFFFFF) | 0x2A000000, 50));
        v.setPadding(dp(c, 10), dp(c, 4), dp(c, 10), dp(c, 4));
        LinearLayout.LayoutParams p = new LinearLayout.LayoutParams(ViewGroup.LayoutParams.WRAP_CONTENT,
                ViewGroup.LayoutParams.WRAP_CONTENT);
        p.setMargins(dp(c, 6), 0, 0, 0);
        v.setLayoutParams(p);
        return v;
    }

    public static TextView button(Context c, CharSequence t, int style) {
        TextView v = text(c, t, 14, style == GHOST ? TEXT : 0xFFFFFFFF, true);
        GradientDrawable bg;
        if (style == PRIMARY) {
            bg = new GradientDrawable(GradientDrawable.Orientation.LEFT_RIGHT, new int[]{ACCENT, ACCENT2});
            bg.setCornerRadius(dp(c, 12));
        } else {
            bg = round(c, style == DANGER ? 0xFF7A2030 : CARD2, 12);
            bg.setStroke(dp(c, 1), style == DANGER ? BAD : LINE);
        }
        v.setBackground(new RippleDrawable(ColorStateList.valueOf(0x55FFFFFF), bg, null));
        v.setGravity(Gravity.CENTER);
        v.setPadding(dp(c, 16), dp(c, 10), dp(c, 16), dp(c, 10));
        v.setClickable(true);
        v.setFocusable(true);
        LinearLayout.LayoutParams p = new LinearLayout.LayoutParams(ViewGroup.LayoutParams.WRAP_CONTENT,
                ViewGroup.LayoutParams.WRAP_CONTENT);
        p.setMargins(dp(c, 8), 0, 0, 0);
        v.setLayoutParams(p);
        return v;
    }

    public static View divider(Context c) {
        View v = new View(c);
        v.setBackgroundColor(LINE);
        v.setLayoutParams(lp(c, ViewGroup.LayoutParams.MATCH_PARENT, dp(c, 1), 0, 4, 0, 4));
        return v;
    }

    public static EditText input(Context c, String hint, int type) {
        EditText v = new EditText(c);
        v.setHint(hint);
        v.setInputType(type == 0 ? InputType.TYPE_CLASS_TEXT : type);
        v.setTextColor(TEXT);
        v.setHintTextColor(MUTED);
        v.setSingleLine(true);
        return v;
    }

    /** A number tile ("23" / "PLAYERS"). */
    public static LinearLayout stat(Context c, CharSequence value, CharSequence name, int color) {
        LinearLayout v = column(c);
        v.setBackground(round(c, CARD2, 14));
        v.setPadding(dp(c, 12), dp(c, 12), dp(c, 12), dp(c, 12));
        v.addView(text(c, value, 22, color, true));
        TextView n = text(c, name.toString().toUpperCase(), 10, MUTED, true);
        n.setLetterSpacing(0.1f);
        v.addView(n);
        LinearLayout.LayoutParams p = new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1);
        p.setMargins(dp(c, 4), dp(c, 4), dp(c, 4), dp(c, 4));
        v.setLayoutParams(p);
        return v;
    }

    /** A whole screen: a header (title, subtitle, action buttons) over a pull-to-refresh list of cards. */
    public static final class Screen {
        public LinearLayout root, content, actions;
        public TextView title, subtitle;
        public SwipeRefreshLayout swipe;
        public ScrollView scroll;
    }

    public static Screen screen(Activity a, String title, String subtitle) {
        Screen s = new Screen();
        a.getWindow().setStatusBarColor(BG);
        a.getWindow().setNavigationBarColor(BG);
        s.root = column(a);
        s.root.setBackgroundColor(BG);
        s.root.setFitsSystemWindows(true);
        LinearLayout head = new LinearLayout(a);
        head.setGravity(Gravity.CENTER_VERTICAL);
        head.setPadding(dp(a, 18), dp(a, 14), dp(a, 12), dp(a, 12));
        LinearLayout titles = column(a);
        s.title = text(a, title, 24, TEXT, true);
        s.subtitle = text(a, subtitle, 13, MUTED, false);
        titles.addView(s.title);
        titles.addView(s.subtitle);
        head.addView(grow(titles));
        s.actions = new LinearLayout(a);
        s.actions.setGravity(Gravity.CENTER_VERTICAL);
        head.addView(s.actions);
        s.root.addView(head);
        View accent = new View(a);
        accent.setBackground(new GradientDrawable(GradientDrawable.Orientation.LEFT_RIGHT,
                new int[]{ACCENT, ACCENT2, 0x00FF9A3C}));
        s.root.addView(accent, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, dp(a, 2)));
        s.swipe = new SwipeRefreshLayout(a);
        s.swipe.setColorSchemeColors(ACCENT, ACCENT2);
        s.swipe.setProgressBackgroundColorSchemeColor(CARD2);
        ScrollView scroll = s.scroll = new ScrollView(a);
        scroll.setFillViewport(true);
        s.content = column(a);
        s.content.setPadding(dp(a, 14), dp(a, 14), dp(a, 14), dp(a, 24));
        scroll.addView(s.content);
        s.swipe.addView(scroll);
        s.root.addView(s.swipe, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, 0, 1));
        a.setContentView(s.root);
        return s;
    }

    /** A round button in the header ("＋", "↻"). */
    public static TextView action(Screen s, String glyph, View.OnClickListener onClick) {
        Context c = s.root.getContext();
        TextView v = text(c, glyph, 18, TEXT, true);
        v.setGravity(Gravity.CENTER);
        v.setBackground(new RippleDrawable(ColorStateList.valueOf(0x55FFFFFF), round(c, CARD2, 50), null));
        LinearLayout.LayoutParams p = new LinearLayout.LayoutParams(dp(c, 42), dp(c, 42));
        p.setMargins(dp(c, 8), 0, 0, 0);
        v.setLayoutParams(p);
        v.setOnClickListener(onClick);
        s.actions.addView(v);
        return v;
    }

    /** A centred note with an optional button – empty lists, errors. */
    public static LinearLayout note(Context c, String emoji, String title, String body) {
        LinearLayout v = card(c);
        v.setGravity(Gravity.CENTER_HORIZONTAL);
        TextView e = text(c, emoji, 40, TEXT, false);
        e.setGravity(Gravity.CENTER);
        v.addView(e);
        TextView t = text(c, title, 18, TEXT, true);
        t.setGravity(Gravity.CENTER);
        v.addView(t);
        TextView b = text(c, body, 14, MUTED, false);
        b.setGravity(Gravity.CENTER);
        b.setPadding(0, dp(c, 6), 0, dp(c, 10));
        v.addView(b);
        return v;
    }

    public static void message(Activity a, String text) {
        if (text == null || text.isEmpty() || a.isFinishing()) return;
        new AlertDialog.Builder(a).setMessage(text).setPositiveButton("OK", null).show();
    }

    public static LinearLayout form(Context c, View... fields) {
        LinearLayout box = column(c);
        box.setPadding(dp(c, 20), dp(c, 8), dp(c, 20), 0);
        for (View f : fields) box.addView(f);
        return box;
    }

    /** "2 h 15 min" until a Unix time (seconds). */
    public static String until(long at) {
        long left = at - System.currentTimeMillis() / 1000;
        if (left <= 0) return "now";
        long d = left / 86400, h = (left % 86400) / 3600, m = (left % 3600) / 60, sec = left % 60;
        if (d > 0) return d + " d " + h + " h";
        if (h > 0) return h + " h " + m + " min";
        return m > 0 ? m + " min " + sec + " s" : sec + " s";
    }
}
