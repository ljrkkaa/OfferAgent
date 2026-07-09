import type { Metadata } from "next";

import "../globals.css";

export const metadata: Metadata = {
    title: "OfferAgent - Search",
    description:
        "Find anything in documents you've shared with OfferAgent using natural language queries.",
    icons: {
        icon: "/static/assets/icons/khoj_lantern.ico",
        apple: "/static/assets/icons/khoj_lantern_256x256.png",
    },
    openGraph: {
        siteName: "OfferAgent",
        title: "OfferAgent - Search",
        description: "Your local interview knowledge base.",
        url: "http://localhost:12805/search",
        type: "website",
        images: [
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
    return <>{children}</>;
}
