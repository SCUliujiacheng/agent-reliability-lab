package dev.portableapp.shell;

import static org.junit.Assert.*;
import static androidx.test.espresso.intent.Intents.intended;
import static androidx.test.espresso.intent.Intents.intending;
import static androidx.test.espresso.intent.matcher.IntentMatchers.hasAction;
import static androidx.test.espresso.Espresso.onView;
import static androidx.test.espresso.action.ViewActions.click;
import static androidx.test.espresso.matcher.ViewMatchers.withId;
import static androidx.test.espresso.matcher.RootMatchers.withDecorView;
import static org.hamcrest.CoreMatchers.is;

import android.app.Activity;
import android.app.ActivityManager;
import android.app.Instrumentation;
import android.app.KeyguardManager;
import android.content.Context;
import android.content.Intent;
import android.net.Uri;
import android.os.ParcelFileDescriptor;
import android.os.PowerManager;
import android.view.View;
import android.webkit.WebView;
import androidx.test.core.app.ActivityScenario;
import androidx.test.core.app.ApplicationProvider;
import androidx.test.espresso.intent.Intents;
import androidx.test.ext.junit.runners.AndroidJUnit4;
import androidx.test.platform.app.InstrumentationRegistry;
import com.chaquo.python.PyObject;
import com.chaquo.python.Python;
import org.json.JSONObject;
import org.junit.Test;
import org.junit.runner.RunWith;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.UUID;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicReference;

/** Exercises the packaged Python/runtime/assets, never a test server or Javascript bridge. */
@RunWith(AndroidJUnit4.class)
public final class OfflineAppTest {
    private PortableRuntime.Session awaitSession() throws Exception {
        Context context = ApplicationProvider.getApplicationContext();
        return PortableRuntime.obtain(context);
    }

    private static String evaluate(ActivityScenario<MainActivity> scenario, String script) throws Exception {
        AtomicReference<String> result = new AtomicReference<>();
        CountDownLatch done = new CountDownLatch(1);
        scenario.onActivity(activity -> ((WebView) activity.findViewById(MainActivity.WEB_VIEW_ID))
                .evaluateJavascript(script, value -> { result.set(value); done.countDown(); }));
        assertTrue("WebView did not evaluate JavaScript", done.await(10, TimeUnit.SECONDS));
        return result.get();
    }

    private static void awaitPage(ActivityScenario<MainActivity> scenario) throws Exception {
        long until = System.nanoTime() + TimeUnit.SECONDS.toNanos(40);
        while (System.nanoTime() < until) {
            try {
                String ready = evaluate(scenario, "document.readyState === 'complete' && document.body.innerText.trim().length > 10 && document.querySelector('button,input,textarea,select') !== null");
                if ("true".equals(ready)) return;
            } catch (NullPointerException ignored) { }
            Thread.sleep(100);
        }
        fail("Packaged application did not render in the real WebView");
    }

    private static void awaitPaint(ActivityScenario<MainActivity> scenario) throws Exception {
        // JavaScript completion is not a renderer-frame acknowledgement. Wait until
        // the injected control can be drawn before sending a real touch event.
        CountDownLatch painted = new CountDownLatch(1);
        scenario.onActivity(activity -> {
            WebView view = activity.findViewById(MainActivity.WEB_VIEW_ID);
            view.postVisualStateCallback(1, new WebView.VisualStateCallback() {
                @Override public void onComplete(long requestId) { painted.countDown(); }
            });
            view.invalidate();
        });
        assertTrue("WebView did not commit the picker control", painted.await(15, TimeUnit.SECONDS));
        InstrumentationRegistry.getInstrumentation().waitForIdleSync();
    }

    private static String shellOutput(String command) throws Exception {
        try (InputStream input = new ParcelFileDescriptor.AutoCloseInputStream(
                InstrumentationRegistry.getInstrumentation().getUiAutomation().executeShellCommand(command))) {
            ByteArrayOutputStream output = new ByteArrayOutputStream();
            BoundaryPolicy.copyBounded(input, output, 1024 * 1024);
            return output.toString(StandardCharsets.UTF_8.name());
        }
    }

    private static String focusDiagnostics(String state) {
        StringBuilder result = new StringBuilder(state);
        try {
            // Only window/power metadata, never page contents or session cookies.
            String output = shellOutput("dumpsys window windows") + shellOutput("dumpsys power");
            for (String line : output.split("\\r?\\n")) {
                if (line.contains("mCurrentFocus") || line.contains("mFocusedApp")
                        || line.contains("mWakefulness=") || line.contains("mInteractive=")) {
                    result.append('\n').append(line.trim());
                }
            }
        } catch (Exception failure) {
            result.append("; window diagnostics unavailable: ").append(failure.getClass().getSimpleName());
        }
        return result.toString();
    }

    private static void touchWebView(ActivityScenario<MainActivity> scenario) throws Exception {
        Context context = ApplicationProvider.getApplicationContext();
        PowerManager power = context.getSystemService(PowerManager.class);
        KeyguardManager keyguard = context.getSystemService(KeyguardManager.class);
        // Test-device setup only. Never change the production Activity's screen flags.
        if (!power.isInteractive()) shellOutput("input keyevent KEYCODE_WAKEUP");
        if (keyguard.isKeyguardLocked()) shellOutput("wm dismiss-keyguard");
        AtomicReference<View> decor = new AtomicReference<>();
        scenario.onActivity(activity -> {
            decor.set(activity.getWindow().getDecorView());
            for (ActivityManager.AppTask task : activity.getSystemService(ActivityManager.class).getAppTasks()) {
                if (task.getTaskInfo().id == activity.getTaskId()) {
                    task.moveToFront();
                    break;
                }
            }
        });
        long until = System.nanoTime() + TimeUnit.SECONDS.toNanos(15);
        AtomicReference<String> lastState = new AtomicReference<>("Activity window not observed");
        while (System.nanoTime() < until) {
            AtomicReference<Boolean> ready = new AtomicReference<>(false);
            scenario.onActivity(activity -> {
                View root = activity.getWindow().getDecorView();
                View view = activity.findViewById(MainActivity.WEB_VIEW_ID);
                boolean focused = root.hasWindowFocus();
                boolean layout = root.isLayoutRequested();
                boolean shown = view.isShown() && view.getWidth() > 0 && view.getHeight() > 0;
                lastState.set("interactive=" + power.isInteractive() + "; keyguardLocked="
                        + keyguard.isKeyguardLocked() + "; windowFocus=" + focused
                        + "; layoutRequested=" + layout + "; webViewShown=" + shown);
                ready.set(power.isInteractive() && !keyguard.isKeyguardLocked() && focused && !layout && shown);
                decor.set(root);
            });
            if (ready.get()) {
                awaitPaint(scenario);
                // Pin the scenario's current window, not a leftover Activity root.
                try {
                    onView(withId(MainActivity.WEB_VIEW_ID)).inRoot(withDecorView(is(decor.get()))).perform(click());
                } catch (RuntimeException | AssertionError failure) {
                    throw new AssertionError("Real WebView touch failed after readiness: "
                            + focusDiagnostics(lastState.get()), failure);
                }
                return;
            }
            Thread.sleep(100);
        }
        fail("The test Activity never became touch-ready: " + focusDiagnostics(lastState.get()));
    }

    private static void awaitPicker(String action) throws Exception {
        long until = System.nanoTime() + TimeUnit.SECONDS.toNanos(15);
        while (System.nanoTime() < until) {
            InstrumentationRegistry.getInstrumentation().waitForIdleSync();
            for (Intent intent : Intents.getIntents()) {
                if (action.equals(intent.getAction())) {
                    intended(hasAction(action));
                    return;
                }
            }
            Thread.sleep(100);
        }
        fail("Native picker was not requested: " + action);
    }

    private static Reply request(PortableRuntime.Session session, String path, String cookie, String body) throws Exception {
        HttpURLConnection connection = (HttpURLConnection) new URL(session.origin + path).openConnection();
        connection.setConnectTimeout(5000);
        connection.setReadTimeout(10000);
        connection.setInstanceFollowRedirects(false);
        if (cookie != null) connection.setRequestProperty("Cookie", cookie);
        if (body != null) {
            connection.setRequestMethod("POST");
            connection.setRequestProperty("Content-Type", "application/json");
            connection.setRequestProperty("X-Ledger-Request", "1");
            connection.setDoOutput(true);
            try (java.io.OutputStream output = connection.getOutputStream()) { output.write(body.getBytes(StandardCharsets.UTF_8)); }
        }
        int status = connection.getResponseCode();
        String content;
        try (InputStream input = status >= 400 ? connection.getErrorStream() : connection.getInputStream()) {
            ByteArrayOutputStream bytes = new ByteArrayOutputStream();
            if (input != null) BoundaryPolicy.copyBounded(input, bytes, 32L * 1024 * 1024);
            content = bytes.toString(StandardCharsets.UTF_8.name());
        } finally { connection.disconnect(); }
        return new Reply(status, content);
    }

    @Test public void realPythonAndWebViewRequireSessionAndSurviveActivityRecreation() throws Exception {
        try (ActivityScenario<MainActivity> scenario = ActivityScenario.launch(MainActivity.class)) {
            PortableRuntime.Session session = awaitSession();
            awaitPage(scenario);
            assertEquals(200, request(session, "/__health", session.cookiePair, null).status);
            assertTrue(request(session, "/__health", null, null).status >= 400);
            assertTrue(request(session, "/", "invalid=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx", null).status >= 400);
            assertEquals("true", evaluate(scenario, "location.origin === " + JSONObject.quote(session.origin)));
            assertEquals("\"\"", evaluate(scenario, "document.cookie"));
            evaluate(scenario, "window.__offlineHealth=0;fetch('/__health').then(r=>window.__offlineHealth=r.status).catch(()=>window.__offlineHealth=-1);true");
            long healthDeadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(10);
            String browserStatus = "0";
            while (System.nanoTime() < healthDeadline) {
                browserStatus = evaluate(scenario, "window.__offlineHealth");
                if (!browserStatus.equals("0")) break;
                Thread.sleep(100);
            }
            assertEquals("The actual WebView could not authenticate its fetch", "200", browserStatus);
            assertTrue(session.dataDirectory.getCanonicalPath().startsWith(
                    ApplicationProvider.<Context>getApplicationContext().getNoBackupFilesDir().getCanonicalPath() + File.separator));
            scenario.recreate();
            awaitPage(scenario);
            assertSame("Activity recreation restarted the Python service", session, awaitSession());
            assertEquals(200, request(session, "/__health", session.cookiePair, null).status);
            assertFalse(session.policy.isInternal("https://example.com/"));
        }
    }

    @Test public void ledgerBackupRestoresThroughPackagedPythonAndSurvivesRestart() throws Exception {
        Context context = ApplicationProvider.getApplicationContext();
        try (ActivityScenario<MainActivity> scenario = ActivityScenario.launch(MainActivity.class)) {
            PortableRuntime.Session session = awaitSession();
            org.junit.Assume.assumeTrue(session.metadata.getString("slug").contains("ledger"));
            String note = "android-instrumentation-" + UUID.randomUUID();
            Reply created = request(session, "/api/entries", session.cookiePair,
                    new JSONObject().put("kind", "expense").put("date", "2026-10-12")
                            .put("description", note).put("category", "test").put("amount", "12.34").put("note", "").toString());
            assertEquals(created.body, 201, created.status);
            Reply backup = request(session, "/api/backup", session.cookiePair, null);
            assertEquals(200, backup.status);
            JSONObject parsed = new JSONObject(backup.body);
            assertTrue(parsed.getJSONArray("entries").toString().contains(note));
            SafeFiles.StagedFile download = SafeFiles.download(context, session, session.origin + "/api/backup");
            assertTrue(download.file.length() > 0);
            download.file.delete();
            Python python = Python.getInstance();
            File restoreDirectory = new File(context.getCacheDir(), "restore-test-" + UUID.randomUUID());
            PyObject restored = python.getModule("mobile_app").callAttr("create_app", restoreDirectory.getAbsolutePath(), session.resourceDirectory.getAbsolutePath());
            PyObject payload = python.getModule("json").callAttr("loads", new JSONObject().put("backup", parsed).toString());
            PyObject empty = python.getModule("json").callAttr("loads", "{}");
            PyObject writeHeaders = python.getModule("json").callAttr("loads", "{\"x-ledger-request\":\"1\"}");
            PyObject result = restored.callAttr("handle", "POST", "/api/restore", empty, payload, writeHeaders);
            assertEquals(200, result.asList().get(0).toInt());
            PyObject reopened = python.getModule("mobile_app").callAttr("create_app", restoreDirectory.getAbsolutePath(), session.resourceDirectory.getAbsolutePath());
            PyObject entries = reopened.callAttr("handle", "GET", "/api/entries", empty, null, empty);
            assertTrue(new String(entries.asList().get(2).toJava(byte[].class), StandardCharsets.UTF_8).contains(note));
            assertTrue(request(session, "/api/local-backups", session.cookiePair, null).body.contains("items"));
        }
    }

    @Test public void nativeFileChooserCancellationReturnsToUsablePage() throws Exception {
        Intents.init();
        try (ActivityScenario<MainActivity> scenario = ActivityScenario.launch(MainActivity.class)) {
            awaitSession();
            awaitPage(scenario);
            intending(hasAction(Intent.ACTION_OPEN_DOCUMENT)).respondWith(new Instrumentation.ActivityResult(Activity.RESULT_CANCELED, null));
            evaluate(scenario, "var i=document.createElement('input');i.type='file';i.accept='.json,application/json';i.style='position:fixed;inset:0;width:100%;height:100%;z-index:99999';document.body.appendChild(i);true");
            touchWebView(scenario);
            awaitPicker(Intent.ACTION_OPEN_DOCUMENT);
            assertEquals("true", evaluate(scenario, "document.readyState === 'complete'"));
        } finally { Intents.release(); }
    }

    @Test public void nativeExportPickerCancellationReturnsToUsablePage() throws Exception {
        Intents.init();
        try (ActivityScenario<MainActivity> scenario = ActivityScenario.launch(MainActivity.class)) {
            PortableRuntime.Session session = awaitSession();
            org.junit.Assume.assumeTrue(session.metadata.getString("slug").contains("ledger"));
            awaitPage(scenario);
            intending(hasAction(Intent.ACTION_CREATE_DOCUMENT)).respondWith(new Instrumentation.ActivityResult(Activity.RESULT_CANCELED, null));
            evaluate(scenario, "var a=document.createElement('a');a.href='/api/backup';a.textContent='Export test';a.style='position:fixed;inset:0;width:100%;height:100%;z-index:99999';document.body.appendChild(a);true");
            touchWebView(scenario);
            awaitPicker(Intent.ACTION_CREATE_DOCUMENT);
            assertEquals("true", evaluate(scenario, "document.readyState === 'complete' && location.origin === " + JSONObject.quote(session.origin)));
            assertEquals(200, request(session, "/__health", session.cookiePair, null).status);
        } finally { Intents.release(); }
    }

    @Test public void fileAccessRejectsPrivatePathsAndOversizedSelectedDocuments() throws Exception {
        Context context = ApplicationProvider.getApplicationContext();
        for (Uri uri : new Uri[]{Uri.parse("file:///data/private.json"), Uri.parse("https://example.com/a.json"),
                Uri.parse("content://" + context.getPackageName() + ".imports/anything")}) {
            try { SafeFiles.importDocument(context, uri, true, true); fail("Unsafe selected URI accepted"); }
            catch (java.io.IOException expected) { }
        }
        InputStream oversized = new InputStream() {
            long remaining = 16L * 1024 * 1024 + 1;
            @Override public int read() { return remaining-- > 0 ? 1 : -1; }
            @Override public int read(byte[] bytes) {
                if (remaining <= 0) return -1;
                int count = (int) Math.min(bytes.length, remaining); remaining -= count; return count;
            }
        };
        try { BoundaryPolicy.copyBounded(oversized, new java.io.OutputStream() { @Override public void write(int b) { } }, SafeFiles.IMPORT_LIMIT); fail("16 MiB upload limit not enforced"); }
        catch (BoundaryPolicy.SizeLimitException expected) { }
    }

    @Test public void selectedContentUriIsCopiedAndExportWritesChosenDocument() throws Exception {
        Context context = ApplicationProvider.getApplicationContext();
        Context tests = InstrumentationRegistry.getInstrumentation().getContext();
        String authority = tests.getPackageName() + ".fixtures";
        SafeFiles.StagedFile selected = SafeFiles.importDocument(context,
                Uri.parse("content://" + authority + "/selected.json"), false, true);
        assertEquals("selected.json", selected.name);
        try (InputStream input = context.getContentResolver().openInputStream(selected.uri(context))) {
            ByteArrayOutputStream data = new ByteArrayOutputStream();
            BoundaryPolicy.copyBounded(input, data, 1024);
            assertEquals("{\"fixture\":\"真实文件读写\"}", data.toString(StandardCharsets.UTF_8.name()));
        }
        Uri saved = Uri.parse("content://" + authority + "/saved.json");
        // Replacing a larger prior export must remove its old tail, not corrupt JSON.
        try (java.io.OutputStream old = context.getContentResolver().openOutputStream(saved, "wt")) {
            assertNotNull(old);
            old.write(new byte[4096]);
        }
        SafeFiles.saveDocument(context, selected, saved);
        try (InputStream input = context.getContentResolver().openInputStream(saved)) {
            ByteArrayOutputStream data = new ByteArrayOutputStream();
            BoundaryPolicy.copyBounded(input, data, 1024);
            assertEquals("{\"fixture\":\"真实文件读写\"}", data.toString(StandardCharsets.UTF_8.name()));
        }
        SafeFiles.removeImport(selected);
        try {
            SafeFiles.importDocument(context, Uri.parse("content://" + authority + "/oversized.json"), false, true);
            fail("A provider's false small size bypassed the 16 MiB stream limit");
        } catch (BoundaryPolicy.SizeLimitException expected) { }
    }

    @Test public void closingAndReopeningRotatesTokenAndPreservesPrivateData() throws Exception {
        PortableRuntime.Session before;
        File marker;
        try (ActivityScenario<MainActivity> first = ActivityScenario.launch(MainActivity.class)) {
            before = awaitSession();
            awaitPage(first);
            marker = new File(before.dataDirectory, "instrumentation-persistence-marker");
            try (java.io.FileOutputStream output = new java.io.FileOutputStream(marker)) { output.write(42); }
        }
        boolean closed = false;
        long until = System.nanoTime() + TimeUnit.SECONDS.toNanos(15);
        while (System.nanoTime() < until) {
            try { request(before, "/__health", before.cookiePair, null); }
            catch (java.io.IOException stopped) { closed = true; break; }
            Thread.sleep(100);
        }
        assertTrue("Finishing the last Activity did not stop the local service", closed);
        try (ActivityScenario<MainActivity> next = ActivityScenario.launch(MainActivity.class)) {
            PortableRuntime.Session after = awaitSession();
            awaitPage(next);
            assertNotEquals(before.cookiePair, after.cookiePair);
            assertTrue(request(after, "/__health", before.cookiePair, null).status >= 400);
            assertEquals(200, request(after, "/__health", after.cookiePair, null).status);
            assertTrue(marker.isFile());
            try (java.io.FileInputStream input = new java.io.FileInputStream(marker)) { assertEquals(42, input.read()); }
            marker.delete();
        }
    }

    /** Run seed, install -r the release APK, then verify without clearing app data. */
    @Test public void upgradePreservesPersistedUserData() throws Exception {
        String phase = InstrumentationRegistry.getArguments().getString("upgradePhase", "");
        org.junit.Assume.assumeTrue(phase.equals("seed") || phase.equals("verify"));
        try (ActivityScenario<MainActivity> scenario = ActivityScenario.launch(MainActivity.class)) {
            PortableRuntime.Session session = awaitSession();
            awaitPage(scenario);
            File marker = new File(session.dataDirectory, "instrumentation-upgrade.json");
            if (phase.equals("seed")) {
                JSONObject saved = new JSONObject().put("marker", UUID.randomUUID().toString());
                if (session.metadata.getString("slug").contains("ledger")) {
                    Reply created = request(session, "/api/entries", session.cookiePair,
                            new JSONObject().put("kind", "expense").put("date", "2026-10-12")
                                    .put("description", "Upgrade persistence check").put("category", "test")
                                    .put("amount", "12.34").put("note", saved.getString("marker")).toString());
                    assertEquals(created.body, 201, created.status);
                    saved.put("entry_id", new JSONObject(created.body).getString("id"));
                }
                try (java.io.FileOutputStream output = new java.io.FileOutputStream(marker)) {
                    output.write(saved.toString().getBytes(StandardCharsets.UTF_8));
                    output.getFD().sync();
                }
            } else {
                assertTrue("Reinstall erased the private data directory", marker.isFile());
                JSONObject saved;
                try (InputStream input = new java.io.FileInputStream(marker)) {
                    ByteArrayOutputStream data = new ByteArrayOutputStream();
                    BoundaryPolicy.copyBounded(input, data, 65536);
                    saved = new JSONObject(data.toString(StandardCharsets.UTF_8.name()));
                }
                assertTrue(saved.getString("marker").length() > 20);
                if (saved.has("entry_id")) {
                    Reply reply = request(session, "/api/entries", session.cookiePair, null);
                    assertEquals(200, reply.status);
                    org.json.JSONArray entries = new JSONObject(reply.body).getJSONArray("items");
                    JSONObject found = null;
                    for (int i = 0; i < entries.length(); i++) {
                        JSONObject entry = entries.getJSONObject(i);
                        if (saved.getString("entry_id").equals(entry.getString("id"))) found = entry;
                    }
                    assertNotNull("Reinstall lost the seeded nonempty ledger entry", found);
                    assertEquals(1234, found.getLong("amount_cents"));
                    assertEquals(saved.getString("marker"), found.getString("note"));
                }
            }
        }
    }

    private static final class Reply {
        final int status; final String body;
        Reply(int status, String body) { this.status = status; this.body = body; }
    }
}
