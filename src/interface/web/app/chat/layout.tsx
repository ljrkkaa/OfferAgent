import type { Metadata } from "next";
import "../globals.css";
import { Toaster } from "@/components/ui/toaster";

export const metadata: Metadata = {
    title: "OfferAgent - Chat",
    description:
        "Ask anything. Research answers from across the internet and your documents, draft messages, and summarize documents.",
    icons: {
        icon: "/static/assets/icons/khoj_lantern.ico",
        apple: "/static/assets/icons/khoj_lantern_256x256.png",
    },
    openGraph: {
        siteName: "OfferAgent",
        title: "OfferAgent - Chat",
        description:
            "Ask anything. Research answers from across the internet and your documents, draft messages, and summarize documents.",
        url: "http://localhost:12805/chat",
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
        ],
    },
};

export default function ChildLayout({
    children,
}: Readonly<{
    children: React.ReactNode;
}>) {
    return (
        <>
            {children}
            <Toaster />
        </>
    );
}
