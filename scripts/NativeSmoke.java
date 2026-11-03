import com.elfmcys.ysm.natives.NativeRuntime;
import com.elfmcys.ysm.util.BundledMacLibrary;
import java.nio.file.Path;

/** Runs against the final, reobfuscated JAR on each real host OS. */
public final class NativeSmoke {
    public static void main(String[] args) throws Exception {
        if (!"libysm.dylib".equals(BundledMacLibrary.fileName("Mac OS X", "aarch64")) ||
            !"libysm.dylib".equals(BundledMacLibrary.fileName("Mac OS X", "arm64")) ||
            !"libysm-x86_64.dylib".equals(BundledMacLibrary.fileName("Mac OS X", "amd64")) ||
            BundledMacLibrary.fileName("Linux", "aarch64") != null) {
            throw new AssertionError("Wrong macOS library selection");
        }
        NativeRuntime.load(Path.of(args[0]).toAbsolutePath().toString());
        NativeRuntime.initialize(new NativeRuntime.JavaConfig(3));
        if (NativeRuntime.availableProcessors() < 1) {
            throw new AssertionError("Invalid native processor report");
        }
        if (args[1].startsWith("macos-")) {
            if (BundledMacLibrary.fileName(System.getProperty("os.name"), System.getProperty("os.arch")) == null) {
                throw new AssertionError("macOS runner does not select its embedded library");
            }
            if (!NativeRuntime.macUserDirectories().applicationSupport().isAbsolute()) {
                throw new AssertionError("Invalid native macOS directory report");
            }
        }
        System.out.println("JNI_AND_PLATFORM_SELECTION_OK " + args[1]);
    }
}
