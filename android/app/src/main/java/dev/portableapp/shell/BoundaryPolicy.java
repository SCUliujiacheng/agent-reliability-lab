package dev.portableapp.shell;

import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.io.InterruptedIOException;
import java.net.URI;
import java.net.URISyntaxException;
import java.util.List;
import java.util.ArrayList;
import java.util.Locale;

final class BoundaryPolicy {
    private final URI origin;
    private final List<String> exports;

    BoundaryPolicy(String value, List<String> exports) {
        origin = URI.create(value);
        if (!"http".equals(origin.getScheme()) || !"127.0.0.1".equals(origin.getHost())
                || origin.getPort() <= 0 || origin.getUserInfo() != null
                || !origin.getPath().isEmpty() || origin.getQuery() != null || origin.getFragment() != null) {
            throw new IllegalArgumentException("Invalid local service origin");
        }
        this.exports = new ArrayList<>(exports);
        for (String path : exports) {
            if (!path.startsWith("/") || path.startsWith("//") || path.contains("..")
                    || path.contains("%") || path.contains("?") || path.contains("#") || path.contains("\\")) {
                throw new IllegalArgumentException("Invalid export path");
            }
        }
    }

    boolean isInternal(String value) {
        try {
            URI uri = new URI(value);
            if (!origin.getScheme().equals(uri.getScheme()) || !origin.getHost().equals(uri.getHost())
                    || origin.getPort() != uri.getPort() || uri.getUserInfo() != null) return false;
            String path = uri.getPath();
            String raw = uri.getRawPath().toLowerCase(Locale.ROOT);
            if (path.startsWith("//") || path.contains("\\") || raw.matches(".*%(2f|5c|2e|00).*")) return false;
            for (String segment : path.split("/", -1)) if (segment.equals("..") || segment.equals(".")) return false;
            for (int i = 0; i < path.length(); i++) if (Character.isISOControl(path.charAt(i))) return false;
            return true;
        } catch (URISyntaxException | IllegalArgumentException | NullPointerException ignored) {
            return false;
        }
    }

    boolean isDownload(String value) {
        if (!isInternal(value)) return false;
        URI uri = URI.create(value);
        if (uri.getFragment() != null) return false;
        String path = uri.getPath();
        for (String allowed : exports) {
            if (!allowed.endsWith("/") && allowed.equals(path)) return true;
            if (allowed.endsWith("/") && path.startsWith(allowed)) {
                String tail = path.substring(allowed.length());
                if (!tail.isEmpty() && !tail.contains("/")) return true;
            }
        }
        return false;
    }

    static String safeFilename(String value, String fallback) {
        if (value == null) return fallback;
        String leaf = value.replace('\\', '/');
        leaf = leaf.substring(leaf.lastIndexOf('/') + 1).replaceAll("[\\p{Cntrl}<>:\"|?*]", "_").trim();
        if (leaf.isEmpty() || leaf.equals(".") || leaf.equals("..")) return fallback;
        return leaf.length() > 120 ? leaf.substring(0, 120) : leaf;
    }

    static void copyBounded(InputStream input, OutputStream output, long limit) throws IOException {
        byte[] buffer = new byte[16384];
        long total = 0;
        int count;
        while ((count = input.read(buffer)) != -1) {
            if (Thread.currentThread().isInterrupted()) throw new InterruptedIOException("File operation cancelled");
            total += count;
            if (total > limit) throw new SizeLimitException();
            output.write(buffer, 0, count);
        }
    }

    static final class SizeLimitException extends IOException {
        SizeLimitException() { super("File size exceeds the allowed limit"); }
    }
}
