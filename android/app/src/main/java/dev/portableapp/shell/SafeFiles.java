package dev.portableapp.shell;

import android.content.ContentResolver;
import android.content.Context;
import android.database.Cursor;
import android.net.Uri;
import android.provider.OpenableColumns;

import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.util.Locale;
import java.util.UUID;
import java.util.concurrent.ConcurrentHashMap;

final class SafeFiles {
    static final long IMPORT_LIMIT = 16L * 1024 * 1024;
    private static final long EXPORT_LIMIT = 128L * 1024 * 1024;
    private static final ConcurrentHashMap<String, StagedFile> imports = new ConcurrentHashMap<>();

    static StagedFile importDocument(Context context, Uri source, boolean csv, boolean json) throws IOException {
        if (source == null || !"content".equals(source.getScheme())
                || (context.getPackageName() + ".imports").equals(source.getAuthority())) {
            throw new IOException("Only user-selected documents can be imported");
        }
        ContentResolver resolver = context.getContentResolver();
        String name = "";
        try (Cursor cursor = resolver.query(source, new String[]{OpenableColumns.DISPLAY_NAME, OpenableColumns.SIZE}, null, null, null)) {
            if (cursor != null && cursor.moveToFirst()) {
                int nameIndex = cursor.getColumnIndex(OpenableColumns.DISPLAY_NAME);
                int sizeIndex = cursor.getColumnIndex(OpenableColumns.SIZE);
                if (nameIndex >= 0) name = cursor.getString(nameIndex);
                if (sizeIndex >= 0 && !cursor.isNull(sizeIndex) && cursor.getLong(sizeIndex) > IMPORT_LIMIT) throw new BoundaryPolicy.SizeLimitException();
            }
        }
        name = BoundaryPolicy.safeFilename(name, "document");
        String lower = name.toLowerCase(Locale.ROOT);
        if (!(csv && lower.endsWith(".csv")) && !(json && lower.endsWith(".json"))) throw new IOException("Unsupported import file type");
        String token = UUID.randomUUID().toString();
        File directory = new File(context.getCacheDir(), "selected-imports");
        if (!directory.isDirectory() && !directory.mkdirs()) throw new IOException("Cannot prepare import");
        File file = new File(directory, token);
        boolean success = false;
        try (InputStream input = resolver.openInputStream(source); FileOutputStream output = new FileOutputStream(file)) {
            if (input == null) throw new IOException("Cannot open selected document");
            BoundaryPolicy.copyBounded(input, output, IMPORT_LIMIT);
            output.getFD().sync();
            StagedFile staged = new StagedFile(file, name, lower.endsWith(".csv") ? "text/csv" : "application/json", token);
            imports.put(token, staged);
            success = true;
            return staged;
        } finally {
            if (!success) file.delete();
        }
    }

    static StagedFile imported(Uri uri) {
        String token = uri.getLastPathSegment();
        return token != null && uri.getPathSegments().size() == 1 ? imports.get(token) : null;
    }

    static void removeImport(StagedFile file) {
        imports.remove(file.token);
        file.file.delete();
    }

    static StagedFile download(Context context, PortableRuntime.Session session, String url) throws IOException {
        if (!session.policy.isDownload(url)) throw new IOException("Export destination is not allowed");
        HttpURLConnection connection = (HttpURLConnection) new URL(url).openConnection();
        connection.setInstanceFollowRedirects(false);
        connection.setConnectTimeout(10000);
        connection.setReadTimeout(30000);
        connection.setRequestProperty("Cookie", session.cookiePair);
        connection.setRequestProperty("Origin", session.origin);
        connection.setRequestProperty("Cache-Control", "no-store");
        File file = File.createTempFile("export-", ".pending", context.getCacheDir());
        boolean success = false;
        try {
            if (connection.getResponseCode() != 200) throw new IOException("Export service rejected request");
            if (connection.getContentLengthLong() > EXPORT_LIMIT) throw new BoundaryPolicy.SizeLimitException();
            String contentType = connection.getContentType();
            String mime = contentType == null ? "" : contentType.split(";", 2)[0].trim().toLowerCase(Locale.ROOT);
            if (!mime.equals("text/csv") && !mime.equals("application/json")) throw new IOException("Unexpected export type");
            String fallback = mime.equals("text/csv") ? "export.csv" : "backup.json";
            String name = fallback;
            String disposition = connection.getHeaderField("Content-Disposition");
            if (disposition != null) {
                java.util.regex.Matcher match = java.util.regex.Pattern.compile("(?:^|;)\\s*filename=\"?([^\";]+)", java.util.regex.Pattern.CASE_INSENSITIVE).matcher(disposition);
                if (match.find()) name = BoundaryPolicy.safeFilename(match.group(1), fallback);
            }
            String extension = mime.equals("text/csv") ? ".csv" : ".json";
            if (!name.toLowerCase(Locale.ROOT).endsWith(extension)) name += extension;
            try (InputStream input = connection.getInputStream(); FileOutputStream output = new FileOutputStream(file)) {
                BoundaryPolicy.copyBounded(input, output, EXPORT_LIMIT);
                output.getFD().sync();
            }
            success = true;
            return new StagedFile(file, name, mime, null);
        } finally {
            connection.disconnect();
            if (!success) file.delete();
        }
    }

    static void saveDocument(Context context, StagedFile file, Uri destination) throws IOException {
        if (destination == null || !"content".equals(destination.getScheme())
                || (context.getPackageName() + ".imports").equals(destination.getAuthority())) {
            throw new IOException("Invalid chosen save location");
        }
        try (InputStream input = new FileInputStream(file.file);
             OutputStream output = context.getContentResolver().openOutputStream(destination, "w")) {
            if (output == null) throw new IOException("Cannot open chosen destination");
            BoundaryPolicy.copyBounded(input, output, EXPORT_LIMIT);
            output.flush();
        }
    }

    static final class StagedFile {
        final File file;
        final String name;
        final String mime;
        final String token;
        StagedFile(File file, String name, String mime, String token) {
            this.file = file; this.name = name; this.mime = mime; this.token = token;
        }
        Uri uri(Context context) {
            return new Uri.Builder().scheme("content").authority(context.getPackageName() + ".imports").appendPath(token).build();
        }
    }
}
