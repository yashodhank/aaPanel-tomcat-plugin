## JavaHost v{VERSION}

### Installation

1. Download `javahost-v{VERSION}.zip`.
2. aaPanel → **App Store** → **Third-party** → **Import Plugin**.
3. Select the ZIP file and install.

### Verification

```bash
shasum -a 256 javahost-v{VERSION}.zip   # compare against the published checksum
ls /www/server/panel/plugin/javahost/info.json
```

### Compatibility

- aaPanel / BaoTa-style panels (Python 3 plugin runtime).
- Tomcat 10.1 requires JDK 11+; Tomcat 11 requires JDK 17+ (auto-installed, verified).

### What's New

See [CHANGELOG.md](CHANGELOG.md).

> [!NOTE]
> JavaHost is an independent, clean-room OSS plugin (Apache-2.0). It is NOT an
> official aaPanel product and contains no aaPanel source code; it uses only the
> panel's public third-party plugin API.
