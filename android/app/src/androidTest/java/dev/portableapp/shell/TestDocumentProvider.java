package dev.portableapp.shell;

import android.content.ContentProvider;
import android.content.ContentValues;
import android.database.Cursor;
import android.database.MatrixCursor;
import android.net.Uri;
import android.os.ParcelFileDescriptor;
import android.provider.OpenableColumns;
import java.io.File;
import java.io.FileNotFoundException;
import java.io.FileOutputStream;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.util.Arrays;

/** A real test-only document provider, including a lying size field. */
public final class TestDocumentProvider extends ContentProvider {
    @Override public boolean onCreate() { return true; }
    private File file(Uri uri) throws FileNotFoundException {
        String name = uri.getLastPathSegment();
        if (!Arrays.asList("selected.json", "saved.json", "oversized.json").contains(name)) throw new FileNotFoundException();
        return new File(getContext().getCacheDir(), name);
    }
    @Override public String getType(Uri uri) { return "application/json"; }
    @Override public Cursor query(Uri uri, String[] projection, String selection, String[] args, String order) {
        MatrixCursor result = new MatrixCursor(new String[]{OpenableColumns.DISPLAY_NAME, OpenableColumns.SIZE});
        result.addRow(new Object[]{uri.getLastPathSegment(), 1L});
        return result;
    }
    @Override public ParcelFileDescriptor openFile(Uri uri, String mode) throws FileNotFoundException {
        File file = file(uri);
        try {
            if ("selected.json".equals(file.getName())) {
                try (FileOutputStream output = new FileOutputStream(file)) {
                    output.write("{\"fixture\":\"真实文件读写\"}".getBytes(StandardCharsets.UTF_8));
                }
            } else if ("oversized.json".equals(file.getName())) {
                try (java.io.RandomAccessFile output = new java.io.RandomAccessFile(file, "rw")) { output.setLength(16L * 1024 * 1024 + 1); }
            }
            return ParcelFileDescriptor.open(file, ParcelFileDescriptor.parseMode(mode));
        } catch (IOException issue) { throw new FileNotFoundException(issue.getMessage()); }
    }
    @Override public Uri insert(Uri uri, ContentValues values) { throw new UnsupportedOperationException(); }
    @Override public int update(Uri uri, ContentValues values, String s, String[] a) { throw new UnsupportedOperationException(); }
    @Override public int delete(Uri uri, String s, String[] a) { throw new UnsupportedOperationException(); }
}
