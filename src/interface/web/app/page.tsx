"use client";

import { FormEvent, Suspense, useState } from "react";
import { useRouter } from "next/navigation";

import { useAuthenticatedData } from "@/app/common/auth";
import { buildChatUrl, createNewConversation } from "@/app/common/chatFunctions";
import { Button } from "@/components/ui/button";
import { Separator } from "@/components/ui/separator";
import { SidebarInset, SidebarProvider, SidebarTrigger } from "@/components/ui/sidebar";
import { Textarea } from "@/components/ui/textarea";
import { AppSidebar } from "./components/appSidebar/appSidebar";
import LocalAuthError from "./components/localAuthError/localAuthError";
import Loading from "./components/loading/loading";
import { KhojLogoType } from "./components/logo/khojLogo";

function HomeContent() {
    const router = useRouter();
    const { data: user, isLoading } = useAuthenticatedData();
    const [message, setMessage] = useState("");
    const [submitting, setSubmitting] = useState(false);

    async function startChat(event: FormEvent) {
        event.preventDefault();
        if (!user || submitting) return;
        setSubmitting(true);
        try {
            const conversationId = await createNewConversation();
            router.push(buildChatUrl(conversationId, message.trim()));
        } finally {
            setSubmitting(false);
        }
    }

    if (isLoading) return <Loading />;

    return (
        <SidebarProvider>
            <AppSidebar conversationId="" />
            <SidebarInset>
                <header className="flex h-16 items-center gap-2 border-b px-4">
                    <SidebarTrigger className="-ml-1" />
                    <Separator orientation="vertical" className="mr-2 h-4" />
                    <KhojLogoType className="h-auto w-32 max-w-full" />
                </header>
                <main className="flex min-h-[calc(100vh-4rem)] items-center justify-center p-6">
                    <div className="w-full max-w-3xl space-y-8 text-center">
                        <div>
                            <h1 className="text-4xl font-semibold">Ask your knowledge base</h1>
                            <p className="mt-3 text-muted-foreground">
                                Search evidence, reason with Codex, and review grounded Vault
                                changes.
                            </p>
                        </div>
                        {!user ? (
                            <LocalAuthError />
                        ) : (
                            <form onSubmit={startChat} className="space-y-3">
                                <Textarea
                                    autoFocus
                                    value={message}
                                    onChange={(event) => setMessage(event.target.value)}
                                    placeholder="What do you want to know?"
                                    className="min-h-32 resize-y text-base"
                                />
                                <Button type="submit" disabled={submitting}>
                                    {submitting ? "Starting…" : "Start chat"}
                                </Button>
                            </form>
                        )}
                    </div>
                </main>
            </SidebarInset>
        </SidebarProvider>
    );
}

export default function Home() {
    return (
        <Suspense fallback={<Loading />}>
            <HomeContent />
        </Suspense>
    );
}
