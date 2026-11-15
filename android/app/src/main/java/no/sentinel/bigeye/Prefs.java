package no.sentinel.bigeye;

import android.content.Context;
import android.content.SharedPreferences;

/** The app's few settings: where BigEye is, this phone's key, and the last alert seen. */
final class Prefs {
    static final String DEFAULT_URL = "";  // typed in at pairing, e.g. http://192.168.1.20:8772

    private final SharedPreferences sp;

    Prefs(Context context) {
        sp = context.getSharedPreferences("bigeye", Context.MODE_PRIVATE);
    }

    String url() { return sp.getString("url", DEFAULT_URL); }
    String key() { return sp.getString("key", ""); }
    boolean paired() { return !key().isEmpty(); }
    long lastAlert() { return sp.getLong("last_alert", -1); }
    boolean alertsOn() { return sp.getBoolean("alerts_on", true); }

    void pair(String url, String key) {
        sp.edit().putString("url", url).putString("key", key).putLong("last_alert", -1).putBoolean("alerts_on", true).apply();
    }
    void unpair() { sp.edit().remove("key").apply(); }
    void lastAlert(long id) { sp.edit().putLong("last_alert", id).apply(); }
    void alertsOn(boolean on) { sp.edit().putBoolean("alerts_on", on).apply(); }
}
