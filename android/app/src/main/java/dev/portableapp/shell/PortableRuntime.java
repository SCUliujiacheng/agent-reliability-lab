package dev.portableapp.shell;

import android.content.Context;
import android.content.pm.PackageInfo;
import android.content.res.AssetManager;

import com.chaquo.python.PyObject;
import com.chaquo.python.Python;
import com.chaquo.python.android.AndroidPlatform;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;
import java.util.UUID;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/** A single in-process server; activity recreation never launches a second instance. */
final class PortableRuntime {
    private static Session session;
    private static int attachedActivities;
    private static final ExecutorService cleanup = Executors.newSingleThreadExecutor();

    static synchronized void attach() { attachedActivities++; }

    static void detach(boolean finishing) {
        synchronized (PortableRuntime.class) { attachedActivities--; }
        if (finishing) cleanup.execute(() -> {
            synchronized (PortableRuntime.class) {
                if (attachedActivities == 0 && session != null) {
                    try { Python.getInstance().getModule("portable_host").callAttr("stop"); }
                    finally { session = null; }
                }
            }
        });
    }

    static synchronized Session obtain(Context context) throws Exception {
        if (session != null) return session;
        Context app = context.getApplicationContext();
        if (!Python.isStarted()) Python.start(new AndroidPlatform(app));
        File resources = extractResources(app);
        File data = new File(app.getNoBackupFilesDir(), "data");
        if (!data.isDirectory() && !data.mkdirs()) throw new IOException("Cannot create private data directory");
        JSONObject metadata;
        try (InputStream input = new FileInputStream(new File(resources, "app.json"))) {
            ByteArrayOutputStream output = new ByteArrayOutputStream();
            BoundaryPolicy.copyBounded(input, output, 1024 * 1024);
            metadata = new JSONObject(output.toString(StandardCharsets.UTF_8.name()));
        }
        List<String> exports = new ArrayList<>();
        JSONArray paths = metadata.getJSONArray("export_paths");
        for (int i = 0; i < paths.length(); i++) exports.add(paths.getString(i));
        PyObject result = Python.getInstance().getModule("portable_host").callAttr("start", data.getAbsolutePath(), resources.getAbsolutePath());
        String origin = result.asMap().get(PyObject.fromJava("origin")).toString();
        String cookie = result.asMap().get(PyObject.fromJava("cookie")).toString();
        String pair = cookie.split(";", 2)[0];
        if (!pair.matches("[A-Za-z0-9_-]+=[A-Za-z0-9_.~-]{32,}")
                || !cookie.toLowerCase(java.util.Locale.ROOT).contains("httponly")
                || !cookie.toLowerCase(java.util.Locale.ROOT).contains("samesite=strict")) {
            Python.getInstance().getModule("portable_host").callAttr("stop");
            throw new IOException("Invalid session cookie");
        }
        session = new Session(origin, cookie, pair, new BoundaryPolicy(origin, exports), metadata, data, resources);
        return session;
    }

    private static File extractResources(Context context) throws Exception {
        PackageInfo info = context.getPackageManager().getPackageInfo(context.getPackageName(), 0);
        File root = new File(context.getNoBackupFilesDir(), "resources");
        File destination = new File(root, BuildConfig.VERSION_CODE + "-" + info.lastUpdateTime);
        if (new File(destination, ".complete").isFile()) return destination;
        if (!root.isDirectory() && !root.mkdirs()) throw new IOException("Cannot create resource directory");
        File temporary = new File(root, ".pending-" + UUID.randomUUID());
        try {
            copyAssets(context.getAssets(), "application", temporary);
            if (!new File(temporary, "web/index.html").isFile()) throw new IOException("Bundled UI is missing");
            if (!new File(temporary, ".complete").createNewFile()) throw new IOException("Cannot complete resources");
            if (!temporary.renameTo(destination)) throw new IOException("Cannot publish resources");
            return destination;
        } finally {
            deleteTree(temporary);
        }
    }

    private static void copyAssets(AssetManager assets, String path, File destination) throws IOException {
        String[] children = assets.list(path);
        if (children != null && children.length > 0) {
            if (!destination.isDirectory() && !destination.mkdirs()) throw new IOException("Cannot extract resources");
            for (String name : children) {
                if (name.contains("/") || name.equals(".") || name.equals("..")) throw new IOException("Invalid asset name");
                copyAssets(assets, path + "/" + name, new File(destination, name));
            }
        } else {
            try (InputStream input = assets.open(path); FileOutputStream output = new FileOutputStream(destination)) {
                BoundaryPolicy.copyBounded(input, output, 256L * 1024 * 1024);
            }
        }
    }

    private static void deleteTree(File file) {
        File[] children = file.listFiles();
        if (children != null) for (File child : children) deleteTree(child);
        if (file.exists() && !file.delete()) file.deleteOnExit();
    }

    static final class Session {
        final String origin;
        final String cookie;
        final String cookiePair;
        final BoundaryPolicy policy;
        final JSONObject metadata;
        final File dataDirectory;
        final File resourceDirectory;
        Session(String origin, String cookie, String cookiePair, BoundaryPolicy policy,
                JSONObject metadata, File data, File resources) {
            this.origin = origin; this.cookie = cookie; this.cookiePair = cookiePair;
            this.policy = policy; this.metadata = metadata;
            dataDirectory = data; resourceDirectory = resources;
        }
    }
}
