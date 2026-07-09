import type { Metadata } from "next";
import { noto_sans, noto_sans_arabic } from "@/app/fonts";
import "./globals.css";
import "./globals-print.css";
import { ContentSecurityPolicy } from "./common/layoutHelper";
import { ThemeProvider } from "./components/providers/themeProvider";

export const metadata: Metadata = {
    title: "OfferAgent - Ask Anything",
    description:
        "OfferAgent is a local interview-prep assistant that helps you use your own notes.",
    icons: {
        icon: "/static/assets/icons/khoj_lantern.ico",
        apple: "/static/assets/icons/khoj_lantern_256x256.png",
    },
    manifest: "/static/khoj.webmanifest",
    keywords:
        "interview assistant, offer agent, productivity, AI, local knowledge base, personal assistant",
    openGraph: {
        siteName: "OfferAgent",
        title: "OfferAgent",
        description:
            "OfferAgent is a local interview-prep assistant that helps you use your own notes.",
        url: "http://localhost:12805",
        type: "website",
        images: [
            {
                url: "https://assets.khoj.dev/khoj_hero.png",
                width: 940,
                height: 525,
            },
            {
                url: "https://assets.khoj.dev/khoj_lantern_256x256.png",
                width: 256,
                height: 256,
            },
            {
                url: "https://assets.khoj.dev/khoj_lantern_logomarktype_1200x630.png",
                width: 1200,
                height: 630,
            },
        ],
    },
};

export default function RootLayout({
    children,
}: Readonly<{
    children: React.ReactNode;
}>) {
    return (
        <html
            lang="en"
            className={`${noto_sans.variable} ${noto_sans_arabic.variable}`}
            suppressHydrationWarning
        >
            <head>
                <script
                    dangerouslySetInnerHTML={{
                        __html: `
                            try {
                                if (localStorage.getItem('theme') === 'dark' ||
                                    (!localStorage.getItem('theme') && window.matchMedia('(prefers-color-scheme: dark)').matches)) {
                                    document.documentElement.classList.add('dark');
                                }
                            } catch (e) {}
                        `,
                    }}
                />
            </head>
            <ContentSecurityPolicy />
            <body>
                <ThemeProvider>{children}</ThemeProvider>
            </body>
        </html>
    );
}
