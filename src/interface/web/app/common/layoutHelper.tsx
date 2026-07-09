export function ContentSecurityPolicy() {
    return (
        <meta
            httpEquiv="Content-Security-Policy"
            content="default-src 'self' https://assets.khoj.dev;
               media-src * blob:;
               script-src 'self' https://assets.khoj.dev 'unsafe-inline' 'unsafe-eval';
               connect-src 'self' blob: ws: wss:;
               style-src 'self' https://assets.khoj.dev 'unsafe-inline' https://fonts.googleapis.com;
               img-src 'self' data: blob: https://*.khoj.dev https://*.google.com/ https://*.gstatic.com;
               font-src 'self' https://assets.khoj.dev https://fonts.gstatic.com;
               frame-src 'self';
               child-src 'self';
               object-src 'none';"
        ></meta>
    );
}
