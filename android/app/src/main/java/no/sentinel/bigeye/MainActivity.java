package no.sentinel.bigeye;

import android.Manifest;
import android.annotation.SuppressLint;
import android.app.Activity;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.graphics.Color;
import android.graphics.Typeface;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.PowerManager;
import android.provider.Settings;
import android.text.InputType;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.view.WindowInsets;
import android.webkit.CookieManager;
import android.widget.FrameLayout;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;

import org.json.JSONObject;

import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.net.URLEncoder;
import java.nio.charset.StandardCharsets;

/** Shows the BigEye page (same as in the browser). First time: pair with a code shown on the PC. */
public class MainActivity extends Activity {
    private static final int BG = Color.parseColor("#0b0f14");
    private static final int FG = Color.parseColor("#e6edf3");
    private static final int DIM = Color.parseColor("#8b98a5");
    private static final int ACCENT = Color.parseColor("#4cc2ff");

    private Prefs prefs;
    private WebView web;

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        prefs = new Prefs(this);
        AlertService.channels(this);
        if (Build.VERSION.SDK_INT >= 33 && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS}, 1);
        }
        if (prefs.paired()) {
            if (prefs.alertsOn()) AlertService.start(this);
            showPage();
        } else {
            showSetup(null);
        }
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        if (web != null && prefs.paired()) web.reload();  // opened from an alert: show the newest picture
    }

    @Override
    protected void onPause() {
        super.onPause();
        CookieManager.getInstance().flush();  // keep the login; the page's own settings live in its local storage
    }

    /** Android 15 draws apps under the status bar and the navigation buttons: keep the content between them. */
    private void show(View content) {
        if (content.getParent() instanceof ViewGroup) ((ViewGroup) content.getParent()).removeView(content);
        FrameLayout root = new FrameLayout(this);
        root.setBackgroundColor(BG);
        root.addView(content, new FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
        root.setOnApplyWindowInsetsListener((v, insets) -> {
            int l, t, r, b;
            if (Build.VERSION.SDK_INT >= 30) {
                android.graphics.Insets bars = insets.getInsets(WindowInsets.Type.systemBars() | WindowInsets.Type.displayCutout() | WindowInsets.Type.ime());
                l = bars.left; t = bars.top; r = bars.right; b = bars.bottom;
            } else {
                l = insets.getSystemWindowInsetLeft(); t = insets.getSystemWindowInsetTop();
                r = insets.getSystemWindowInsetRight(); b = insets.getSystemWindowInsetBottom();
            }
            v.setPadding(l, t, r, b);
            return insets;
        });
        setContentView(root);
        root.requestApplyInsets();
    }

    @Override
    public void onBackPressed() {
        if (web != null && web.canGoBack()) web.goBack(); else super.onBackPressed();
    }

    // ------------------------------------------------------------------ the page

    @SuppressLint("SetJavaScriptEnabled")
    private void showPage() {
        if (web == null) {
            web = new WebView(this);
            web.setBackgroundColor(BG);
            WebSettings ws = web.getSettings();
            ws.setJavaScriptEnabled(true);
            ws.setDomStorageEnabled(true);
            ws.setMediaPlaybackRequiresUserGesture(false);
            ws.setUserAgentString(ws.getUserAgentString() + " SentinelBigEyeApp/1.0");
            CookieManager.getInstance().setAcceptCookie(true);
            web.setWebViewClient(new WebViewClient() {
                @Override
                public void onReceivedError(WebView view, WebResourceRequest req, WebResourceError err) {
                    if (req.isForMainFrame()) showOffline();
                }

                @Override
                public void onReceivedHttpError(WebView view, WebResourceRequest req, WebResourceResponse resp) {
                    if (req.isForMainFrame() && resp.getStatusCode() == 401) {
                        prefs.unpair();
                        showSetup("This phone is no longer paired. Make a new code on the PC.");
                    }
                }
            });
        }
        show(web);
        String body = "key=" + enc(prefs.key());
        web.postUrl(prefs.url() + "/app/login", body.getBytes(StandardCharsets.UTF_8));
    }

    private void showOffline() {
        LinearLayout box = column();
        box.addView(text("BigEye is not answering", 22, FG, true));
        box.addView(text("The phone must be on the home Wi-Fi, or have the your VPN on.\n\nAddress: " + prefs.url(), 15, DIM, false));
        box.addView(button("Try again", v -> showPage()));
        box.addView(button("Pair again / change address", v -> showSetup(null)));
        show(wrap(box));
    }

    // ------------------------------------------------------------------ pairing

    private void showSetup(String message) {
        LinearLayout box = column();
        box.addView(text("Sentinel-BigEye", 26, FG, true));
        box.addView(text("On the PC, open BigEye and press 📱 Phone app → New code. Then fill in below.", 15, DIM, false));
        EditText url = input(prefs.url(), InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_URI);
        url.setHint("http://<the PC's address>:8772");
        EditText code = input("", InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_FLAG_CAP_CHARACTERS);
        code.setHint("Code (8 letters)");
        box.addView(text("BigEye address", 13, DIM, false));
        box.addView(url);
        box.addView(text("Code from the PC", 13, DIM, false));
        box.addView(code);
        TextView status = text(message == null ? "" : message, 14, Color.parseColor("#ffb340"), false);
        Button go = button("Connect", null);
        go.setOnClickListener(v -> {
            String base = url.getText().toString().trim().replaceAll("/+$", "");
            String c = code.getText().toString().trim().toUpperCase();
            if (!base.startsWith("http")) base = "http://" + base;
            if (c.isEmpty()) { status.setText("Type the code shown on the PC."); return; }
            go.setEnabled(false);
            status.setText("Connecting…");
            final String b = base;
            new Thread(() -> {
                String error = null;
                String key = null;
                try {
                    key = pair(b, c);
                } catch (Exception e) {
                    error = e.getMessage();
                }
                final String k = key, err = error;
                runOnUiThread(() -> {
                    go.setEnabled(true);
                    if (k == null) {
                        status.setText(err == null ? "Could not pair." : err);
                        return;
                    }
                    prefs.pair(b, k);
                    AlertService.start(this);
                    askBatteryExemption();
                    showPage();
                });
            }).start();
        });
        box.addView(go);
        box.addView(status);
        box.addView(text("Alerts come only when they need you: a person while you are Away, a camera gone for "
                + "10 minutes, or BigEye asking for help. They work at home and through the your VPN.", 13, DIM, false));
        show(wrap(box));
    }

    private String pair(String base, String code) throws Exception {
        HttpURLConnection c;
        try {
            c = (HttpURLConnection) new URL(base + "/app/pair").openConnection();
            c.setConnectTimeout(8000);
            c.setReadTimeout(15000);
            c.setDoOutput(true);
            c.setRequestProperty("Content-Type", "application/json");
            JSONObject body = new JSONObject().put("code", code).put("name", Build.MANUFACTURER + " " + Build.MODEL);
            try (OutputStream out = c.getOutputStream()) { out.write(body.toString().getBytes(StandardCharsets.UTF_8)); }
            int status = c.getResponseCode();
            InputStream in = status < 400 ? c.getInputStream() : c.getErrorStream();
            String reply = in == null ? "{}" : new String(in.readAllBytes(), StandardCharsets.UTF_8);
            JSONObject r = new JSONObject(reply.isEmpty() ? "{}" : reply);
            if (status != 200) throw new Exception(r.optString("error", "BigEye said HTTP " + status));
            return r.getString("key");
        } catch (java.io.IOException e) {
            throw new Exception("Can't reach " + base + " (home Wi-Fi or your VPN on?)");
        }
    }

    /** Without this, Android's battery saver cuts the connection and alerts arrive late. */
    @SuppressLint("BatteryLife")
    private void askBatteryExemption() {
        PowerManager pm = getSystemService(PowerManager.class);
        if (pm != null && !pm.isIgnoringBatteryOptimizations(getPackageName())) {
            try {
                startActivity(new Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS, Uri.parse("package:" + getPackageName())));
            } catch (Exception ignored) { }
        }
    }

    // ------------------------------------------------------------------ small view helpers

    private LinearLayout column() {
        LinearLayout box = new LinearLayout(this);
        box.setOrientation(LinearLayout.VERTICAL);
        int pad = dp(24);
        box.setPadding(pad, dp(48), pad, pad);
        box.setBackgroundColor(BG);
        return box;
    }

    private View wrap(LinearLayout box) {
        ScrollView sv = new ScrollView(this);
        sv.setBackgroundColor(BG);
        sv.setFillViewport(true);
        sv.addView(box, new ViewGroup.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));
        return sv;
    }

    private TextView text(String s, int sp, int color, boolean bold) {
        TextView t = new TextView(this);
        t.setText(s);
        t.setTextSize(sp);
        t.setTextColor(color);
        if (bold) t.setTypeface(Typeface.DEFAULT_BOLD);
        t.setPadding(0, dp(8), 0, dp(8));
        return t;
    }

    private EditText input(String value, int type) {
        EditText e = new EditText(this);
        e.setText(value);
        e.setInputType(type);
        e.setTextColor(FG);
        e.setHintTextColor(DIM);
        e.setSingleLine(true);
        return e;
    }

    private Button button(String label, View.OnClickListener click) {
        Button b = new Button(this);
        b.setText(label);
        b.setAllCaps(false);
        b.setTextColor(BG);
        b.setBackgroundColor(ACCENT);
        b.setGravity(Gravity.CENTER);
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, dp(48));
        lp.topMargin = dp(12);
        b.setLayoutParams(lp);
        if (click != null) b.setOnClickListener(click);
        return b;
    }

    private int dp(int v) { return Math.round(v * getResources().getDisplayMetrics().density); }

    private static String enc(String s) {
        try { return URLEncoder.encode(s, "UTF-8"); } catch (Exception e) { return s; }
    }
}
