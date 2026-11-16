package no.sentinel.bigeyecam;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.os.Build;

/** Starts filming again after a restart or an app update, if it was on before. */
public class BootReceiver extends BroadcastReceiver {
    @Override
    public void onReceive(Context ctx, Intent intent) {
        if (!ctx.getSharedPreferences("cam", Context.MODE_PRIVATE).getBoolean("on", false)) return;
        if (Build.VERSION.SDK_INT >= 34) {   // Android 14+: a camera may not be started from the background
            askToOpen(ctx);
            return;
        }
        Intent i = new Intent(ctx, CamService.class);
        try {
            if (Build.VERSION.SDK_INT >= 26) ctx.startForegroundService(i);
            else ctx.startService(i);
        } catch (Exception e) {
            askToOpen(ctx);
        }
    }

    /** A notification: "tap to start the camera again" (the app starts filming when it opens). */
    static void askToOpen(Context ctx) {
        NotificationManager nm = (NotificationManager) ctx.getSystemService(Context.NOTIFICATION_SERVICE);
        Notification.Builder b;
        if (Build.VERSION.SDK_INT >= 26) {
            nm.createNotificationChannel(new NotificationChannel("resume", "Camera stopped", NotificationManager.IMPORTANCE_HIGH));
            b = new Notification.Builder(ctx, "resume");
        } else {
            b = new Notification.Builder(ctx);
        }
        int pf = Build.VERSION.SDK_INT >= 23 ? PendingIntent.FLAG_IMMUTABLE : 0;
        PendingIntent open = PendingIntent.getActivity(ctx, 1, new Intent(ctx, MainActivity.class), pf);
        nm.notify(2, b.setSmallIcon(R.drawable.ic_stat).setContentTitle("BigEye Cam stopped")
                .setContentText("Tap to start the camera again").setContentIntent(open).setAutoCancel(true).build());
    }
}
