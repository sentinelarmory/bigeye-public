package no.sentinel.bigeye;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.pm.ServiceInfo;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.net.ConnectivityManager;
import android.net.Network;
import android.os.Build;
import android.os.IBinder;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.net.URLEncoder;
import java.nio.charset.StandardCharsets;

/**
 * Keeps one long-poll open to BigEye (/app/alerts) and turns the alerts that need the owner into phone
 * notifications: a person while Away, a camera gone for 10 minutes, the guard asking for help.
 * Talks only to the owner's own BigEye (home Wi-Fi or your VPN); no cloud push service.
 */
public class AlertService extends Service {
    static final String STOP = "no.sentinel.bigeye.STOP";
    private static final String CH_ALERTS = "alerts";
    private static final String CH_WATCH = "watch";
    private static final int ID_WATCH = 1;

    private volatile boolean running;
    private Thread worker;
    private ConnectivityManager.NetworkCallback netCallback;
    private String lastState = "";

    static void start(Context context) {
        Intent i = new Intent(context, AlertService.class);
        if (Build.VERSION.SDK_INT >= 26) context.startForegroundService(i); else context.startService(i);
    }

    static void channels(Context context) {
        NotificationManager nm = context.getSystemService(NotificationManager.class);
        NotificationChannel alerts = new NotificationChannel(CH_ALERTS, "Alerts that need you", NotificationManager.IMPORTANCE_HIGH);
        alerts.setDescription("A person while Away, a camera offline, or BigEye asking for help");
        alerts.enableVibration(true);
        nm.createNotificationChannel(alerts);
        NotificationChannel watch = new NotificationChannel(CH_WATCH, "Listening for alerts", NotificationManager.IMPORTANCE_MIN);
        watch.setDescription("The quiet, always-there notice that BigEye alerts are on (Android requires it)");
        watch.setShowBadge(false);
        nm.createNotificationChannel(watch);
    }

    @Override
    public IBinder onBind(Intent intent) { return null; }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        Prefs prefs = new Prefs(this);
        channels(this);
        Notification n = watchNotice("Starting…");
        if (Build.VERSION.SDK_INT >= 34) {
            startForeground(ID_WATCH, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE);
        } else {
            startForeground(ID_WATCH, n);
        }
        if (intent != null && STOP.equals(intent.getAction())) {
            prefs.alertsOn(false);
            stopSelf();
            return START_NOT_STICKY;
        }
        if (!prefs.paired() || !prefs.alertsOn()) {
            stopSelf();
            return START_NOT_STICKY;
        }
        if (worker == null || !worker.isAlive()) {
            running = true;
            worker = new Thread(this::loop, "bigeye-alerts");
            worker.start();
            watchNetwork();
        }
        return START_STICKY;
    }

    @Override
    public void onDestroy() {
        running = false;
        if (worker != null) worker.interrupt();
        if (netCallback != null) {
            try { getSystemService(ConnectivityManager.class).unregisterNetworkCallback(netCallback); } catch (Exception ignored) { }
        }
        super.onDestroy();
    }

    /** A new network (home Wi-Fi, VPN up) -> try again right away instead of waiting out the pause. */
    private void watchNetwork() {
        netCallback = new ConnectivityManager.NetworkCallback() {
            @Override public void onAvailable(Network network) { if (worker != null) worker.interrupt(); }
        };
        try { getSystemService(ConnectivityManager.class).registerDefaultNetworkCallback(netCallback); } catch (Exception ignored) { }
    }

    private void loop() {
        Prefs prefs = new Prefs(this);
        long pause = 10_000;
        while (running) {
            try {
                long after = prefs.lastAlert();
                String q = after < 0 ? "after=-1" : "after=" + after + "&wait=50";
                JSONObject r = new JSONObject(new String(get(prefs, "/app/alerts?" + q, 70_000), StandardCharsets.UTF_8));
                long last = r.getLong("last");
                if (after >= 0) {
                    JSONArray list = r.getJSONArray("alerts");
                    for (int i = 0; i < list.length(); i++) show(prefs, list.getJSONObject(i));
                }
                prefs.lastAlert(last);
                state("Alerts on · connected to BigEye");
                pause = 10_000;
            } catch (NotPaired e) {
                state("This phone is no longer paired. Open BigEye to pair it again.");
                prefs.unpair();
                running = false;
                stopSelf();
                return;
            } catch (Exception e) {
                state("Can't reach BigEye right now (away from home with the VPN off?). Trying again…");
                try { Thread.sleep(pause); } catch (InterruptedException ignored) { }
                pause = Math.min(pause * 2, 300_000);
            }
        }
    }

    private void show(Prefs prefs, JSONObject a) {
        String level = a.optString("level", "action");
        if (!"urgent".equals(level) && !"action".equals(level)) return;  // only what needs the owner
        String title = a.optString("title", "BigEye");
        String text = a.optString("text", "");
        Intent open = new Intent(this, MainActivity.class).setFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_CLEAR_TOP);
        open.putExtra("cam", a.optString("cam", ""));
        PendingIntent pi = PendingIntent.getActivity(this, (int) a.optLong("id"), open,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        Notification.Builder b = new Notification.Builder(this, CH_ALERTS)
                .setSmallIcon(R.drawable.ic_stat)
                .setContentTitle(title)
                .setContentText(text)
                .setWhen(a.optLong("t") * 1000)
                .setShowWhen(true)
                .setAutoCancel(true)
                .setContentIntent(pi)
                .setCategory("urgent".equals(level) ? Notification.CATEGORY_ALARM : Notification.CATEGORY_STATUS);
        Bitmap pic = picture(prefs, a.optString("picture", ""));
        if (pic != null) {
            b.setLargeIcon(pic).setStyle(new Notification.BigPictureStyle().bigPicture(pic).setSummaryText(text));
        } else {
            b.setStyle(new Notification.BigTextStyle().bigText(text));
        }
        getSystemService(NotificationManager.class).notify(1000 + (int) (a.optLong("id") % 100000), b.build());
    }

    private Bitmap picture(Prefs prefs, String rel) {
        if (rel == null || rel.isEmpty() || "null".equals(rel)) return null;
        try {
            StringBuilder path = new StringBuilder("/saved");
            for (String part : rel.split("/")) path.append('/').append(URLEncoder.encode(part, "UTF-8").replace("+", "%20"));
            byte[] jpeg = get(prefs, path.toString(), 15_000);
            BitmapFactory.Options o = new BitmapFactory.Options();
            o.inSampleSize = 2;
            return BitmapFactory.decodeByteArray(jpeg, 0, jpeg.length, o);
        } catch (Exception e) {
            return null;
        }
    }

    static final class NotPaired extends Exception { }

    static byte[] get(Prefs prefs, String path, int readTimeout) throws Exception {
        HttpURLConnection c = (HttpURLConnection) new URL(prefs.url() + path).openConnection();
        c.setConnectTimeout(8_000);
        c.setReadTimeout(readTimeout);
        c.setRequestProperty("X-BigEye-Key", prefs.key());
        try {
            int code = c.getResponseCode();
            if (code == 401) throw new NotPaired();
            if (code != 200) throw new java.io.IOException("HTTP " + code);
            try (InputStream in = c.getInputStream()) {
                ByteArrayOutputStream out = new ByteArrayOutputStream();
                byte[] buf = new byte[16384];
                int n;
                while ((n = in.read(buf)) > 0) out.write(buf, 0, n);
                return out.toByteArray();
            }
        } finally {
            c.disconnect();
        }
    }

    private void state(String text) {
        if (text.equals(lastState)) return;
        lastState = text;
        getSystemService(NotificationManager.class).notify(ID_WATCH, watchNotice(text));
    }

    private Notification watchNotice(String text) {
        PendingIntent open = PendingIntent.getActivity(this, 0, new Intent(this, MainActivity.class), PendingIntent.FLAG_IMMUTABLE);
        PendingIntent stop = PendingIntent.getService(this, 1, new Intent(this, AlertService.class).setAction(STOP), PendingIntent.FLAG_IMMUTABLE);
        return new Notification.Builder(this, CH_WATCH)
                .setSmallIcon(R.drawable.ic_stat)
                .setContentTitle("BigEye")
                .setContentText(text)
                .setOngoing(true)
                .setContentIntent(open)
                .addAction(new Notification.Action.Builder(null, "Turn alerts off", stop).build())
                .build();
    }
}
