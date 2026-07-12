"use client";

/* eslint-disable @next/next/no-img-element -- ponytail: dynamic base64 previews and favicons are not Next Image assets. */

import { useEffect, useState } from "react";

import { ArrowRight, Note, Clipboard, Check } from "@phosphor-icons/react";

import markdownIt from "markdown-it";
const md = new markdownIt({
    html: true,
    linkify: true,
    typographer: true,
});

import { Context, WebPage, OnlineContext } from "../chatMessage/chatMessage";
import { Card } from "@/components/ui/card";

import {
    Sheet,
    SheetContent,
    SheetDescription,
    SheetHeader,
    SheetTitle,
    SheetTrigger,
} from "@/components/ui/sheet";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import DOMPurify from "dompurify";
import { getIconFromFilename } from "@/app/common/iconUtils";
import Link from "next/link";
import { Button } from "@/components/ui/button";

interface NotesContextReferenceData {
    title: string;
    content: string;
}

interface NotesContextReferenceCardProps extends NotesContextReferenceData {
    showFullContent: boolean;
}

function extractSnippet(props: NotesContextReferenceCardProps): string {
    const hierarchicalFileExtensions = ["org", "md", "markdown"];
    const extension = props.title.split(".").pop() || "";
    const cleanContent = hierarchicalFileExtensions.includes(extension)
        ? props.content.split("\n").slice(1).join("\n")
        : props.content;
    return props.showFullContent
        ? DOMPurify.sanitize(md.render(cleanContent))
        : DOMPurify.sanitize(cleanContent);
}

function NotesContextReferenceCard(props: NotesContextReferenceCardProps) {
    const fileIcon = getIconFromFilename(
        props.title || ".txt",
        "w-6 h-6 text-muted-foreground inline-flex mr-2",
    );
    const fileName = props.title.split("/").pop() || props.title;
    const snippet = extractSnippet(props);
    const [isHovering, setIsHovering] = useState(false);

    return (
        <>
            <Popover open={isHovering && !props.showFullContent} onOpenChange={setIsHovering}>
                <PopoverTrigger asChild>
                    <Card
                        onMouseEnter={() => setIsHovering(true)}
                        onMouseLeave={() => setIsHovering(false)}
                        className={`${props.showFullContent ? "w-auto bg-muted" : "w-auto"} overflow-hidden break-words text-balance rounded-lg border-none p-2 shadow-none`}
                    >
                        {!props.showFullContent ? (
                            <SimpleIcon type="notes" key={`${props.title}`} />
                        ) : (
                            <>
                                <h3
                                    className={`${props.showFullContent ? "block" : "line-clamp-1"} text-muted-foreground}`}
                                >
                                    {fileIcon}
                                    {props.showFullContent ? props.title : fileName}
                                </h3>
                                <p
                                    className={`text-sm overflow-x-auto block`}
                                    dangerouslySetInnerHTML={{ __html: snippet }}
                                ></p>
                            </>
                        )}
                    </Card>
                </PopoverTrigger>
                <PopoverContent className="w-[400px] mx-2">
                    <Card
                        className={`w-auto overflow-hidden break-words text-balance rounded-lg border-none p-2`}
                    >
                        <h3 className={`line-clamp-2 text-muted-foreground}`}>
                            {fileIcon}
                            {props.title}
                        </h3>
                        <p
                            className={`border-t mt-1 pt-1 text-sm overflow-hidden line-clamp-5`}
                            dangerouslySetInnerHTML={{ __html: snippet }}
                        ></p>
                    </Card>
                </PopoverContent>
            </Popover>
        </>
    );
}

interface OnlineReferenceData {
    title: string;
    description: string;
    link: string;
}

interface OnlineReferenceCardProps extends OnlineReferenceData {
    showFullContent: boolean;
}

function GenericOnlineReferenceCard(props: OnlineReferenceCardProps) {
    const [isHovering, setIsHovering] = useState(false);

    if (!props.link || props.link.split(" ").length > 1) {
        return null;
    }

    let favicon = `https://www.google.com/s2/favicons?domain=globe`;
    let domain = "unknown";
    try {
        domain = new URL(props.link).hostname;
        favicon = `https://www.google.com/s2/favicons?domain=${domain}`;
    } catch (error) {
        console.warn(`Error parsing domain from link: ${props.link}`);
        return null;
    }

    const handleMouseEnter = () => {
        setIsHovering(true);
    };

    const handleMouseLeave = () => {
        setIsHovering(false);
    };

    return (
        <>
            <Popover open={isHovering && !props.showFullContent} onOpenChange={setIsHovering}>
                <PopoverTrigger asChild>
                    <Card
                        onMouseEnter={handleMouseEnter}
                        onMouseLeave={handleMouseLeave}
                        className={`${props.showFullContent ? "w-auto bg-muted" : "w-auto"} overflow-hidden break-words text-balance rounded-lg border-none p-2 shadow-none`}
                    >
                        {!props.showFullContent ? (
                            <SimpleIcon type="online" key={props.title} link={props.link} />
                        ) : (
                            <div className="flex flex-col">
                                {
                                    <Link href={props.link}>
                                        <div className="flex items-center gap-2">
                                            <img
                                                src={favicon}
                                                alt=""
                                                className="!w-4 h-4 flex-shrink-0"
                                            />
                                            <h3
                                                className={`overflow-hidden ${props.showFullContent ? "block" : "line-clamp-1"} text-muted-foreground flex-grow`}
                                            >
                                                {domain}
                                            </h3>
                                        </div>
                                    </Link>
                                }
                                <h3
                                    className={`overflow-hidden ${props.showFullContent ? "block" : "line-clamp-1"} font-bold`}
                                >
                                    {props.title}
                                </h3>
                                <p
                                    className={`overflow-hidden text-sm ${props.showFullContent ? "block" : "line-clamp-2"}`}
                                >
                                    {props.description}
                                </p>
                            </div>
                        )}
                    </Card>
                </PopoverTrigger>
                <PopoverContent className="w-[400px] mx-2">
                    <Card
                        className={`w-auto overflow-hidden break-words text-balance rounded-lg border-none`}
                    >
                        <div className="flex flex-col">
                            <a
                                href={props.link}
                                target="_blank"
                                rel="noreferrer"
                                className="!no-underline px-1"
                            >
                                <div className="flex items-center gap-2">
                                    <img src={favicon} alt="" className="!w-4 h-4 flex-shrink-0" />
                                    <h3
                                        className={`overflow-hidden ${props.showFullContent ? "block" : "line-clamp-2"} text-muted-foreground flex-grow`}
                                    >
                                        {domain}
                                    </h3>
                                </div>
                                <h3
                                    className={`border-t mt-1 pt-1 overflow-hidden ${props.showFullContent ? "block" : "line-clamp-2"} font-bold`}
                                >
                                    {props.title}
                                </h3>
                                <p
                                    className={`overflow-hidden text-sm ${props.showFullContent ? "block" : "line-clamp-5"}`}
                                >
                                    {props.description}
                                </p>
                            </a>
                        </div>
                    </Card>
                </PopoverContent>
            </Popover>
        </>
    );
}

export function constructAllReferences(contextData: Context[], onlineData: OnlineContext) {
    const onlineReferences: OnlineReferenceData[] = [];
    const contextReferences: NotesContextReferenceData[] = [];
    if (onlineData) {
        let localOnlineReferences = [];
        for (const [key, value] of Object.entries(onlineData)) {
            if (value.answerBox) {
                localOnlineReferences.push({
                    title: value.answerBox.title,
                    description: value.answerBox.answer,
                    link: value.answerBox.source,
                });
            }
            if (value.knowledgeGraph) {
                localOnlineReferences.push({
                    title: value.knowledgeGraph.title,
                    description: value.knowledgeGraph.description,
                    link: value.knowledgeGraph.descriptionLink,
                });
            }

            if (value.webpages) {
                // If webpages is of type Array, iterate through it and add each webpage to the localOnlineReferences array
                if (value.webpages instanceof Array) {
                    let webPageResults = value.webpages.map((webPage) => {
                        return {
                            title: webPage.query,
                            description: webPage.snippet,
                            link: webPage.link,
                        };
                    });
                    localOnlineReferences.push(...webPageResults);
                } else {
                    let singleWebpage = value.webpages as WebPage;

                    // If webpages is an object, add the object to the localOnlineReferences array
                    localOnlineReferences.push({
                        title: singleWebpage.query,
                        description: singleWebpage.snippet,
                        link: singleWebpage.link,
                    });
                }
            }

            if (value.organic) {
                let organicResults = value.organic.map((organicContext) => {
                    return {
                        title: organicContext.title,
                        description: organicContext.snippet,
                        link: organicContext.link,
                    };
                });

                localOnlineReferences.push(...organicResults);
            }
        }

        onlineReferences.push(...localOnlineReferences);
    }

    if (contextData) {
        let localContextReferences = contextData.map((context) => {
            if (!context.compiled && context.compiled !== "") {
                const raw = context as unknown;
                const fileContent = typeof raw === "string" ? raw : raw == null ? "" : String(raw);

                const lines = fileContent.split("\n");
                const title = lines[0] && lines[0].trim() ? lines[0] : "(untitled)";
                const content = lines.slice(1).join("\n");
                return {
                    title: title,
                    content: content,
                };
            }
            return {
                title: context.file,
                content: context.compiled,
            };
        });

        contextReferences.push(...localContextReferences);
    }

    return {
        notesReferenceCardData: contextReferences,
        onlineReferenceCardData: onlineReferences,
    };
}

export function formatReferencesAsMarkdown(
    notesReferenceCardData: NotesContextReferenceData[],
    onlineReferenceCardData: OnlineReferenceData[],
): string {
    return [
        ...notesReferenceCardData.map((note) => `- ${note.title}`),
        ...onlineReferenceCardData.map((online) => `- [${online.title}](${online.link})`),
    ].join("\n");
}

interface SimpleIconProps {
    type: string;
    link?: string;
}

function SimpleIcon(props: SimpleIconProps) {
    let favicon = ``;
    let domain = "unknown";

    if (props.link) {
        try {
            domain = new URL(props.link).hostname;
            favicon = `https://www.google.com/s2/favicons?domain=${domain}`;
        } catch (error) {
            console.warn(`Error parsing domain from link: ${props.link}`);
            return null;
        }
    }

    let symbol = null;

    const itemClasses = "!w-4 !h-4 text-muted-foreground inline-flex mr-2 rounded-lg";

    switch (props.type) {
        case "online":
            symbol = <img src={favicon} alt="" className={`${itemClasses}`} />;
            break;
        case "notes":
            symbol = <Note className={`${itemClasses}`} />;
            break;
        default:
            symbol = null;
    }

    if (!symbol) {
        return null;
    }

    return <div className="flex items-center gap-2">{symbol}</div>;
}

export interface TeaserReferenceSectionProps {
    notesReferenceCardData: NotesContextReferenceData[];
    onlineReferenceCardData: OnlineReferenceData[];
    isMobileWidth: boolean;
}

export function TeaserReferencesSection(props: TeaserReferenceSectionProps) {
    const shouldShowShowMoreButton =
        props.notesReferenceCardData.length > 0 || props.onlineReferenceCardData.length > 0;

    const numReferences =
        props.notesReferenceCardData.length + props.onlineReferenceCardData.length;

    if (numReferences === 0) {
        return null;
    }

    return (
        <div className="pt-0 px-4 pb-4">
            <h3 className="inline-flex items-center">
                <div className="text-gray-400 m-2">{numReferences} sources</div>
                <div className={`flex flex-wrap gap-2 w-auto m-2`}>
                    {shouldShowShowMoreButton && (
                        <ReferencePanel
                            notesReferenceCardData={props.notesReferenceCardData}
                            onlineReferenceCardData={props.onlineReferenceCardData}
                            isMobileWidth={props.isMobileWidth}
                        />
                    )}
                </div>
            </h3>
        </div>
    );
}

interface ReferencePanelDataProps {
    notesReferenceCardData: NotesContextReferenceData[];
    onlineReferenceCardData: OnlineReferenceData[];
    isMobileWidth: boolean;
}

export default function ReferencePanel(props: ReferencePanelDataProps) {
    const [numTeaserSlots, setNumTeaserSlots] = useState(3);

    const [copyReferencesSuccess, setCopyReferencesSuccess] = useState(false);

    useEffect(() => {
        setNumTeaserSlots(props.isMobileWidth ? 3 : 5);
    }, [props.isMobileWidth]);

    useEffect(() => {
        if (copyReferencesSuccess) {
            setTimeout(() => {
                setCopyReferencesSuccess(false);
            }, 1000);
        }
    }, [copyReferencesSuccess]);

    if (!props.notesReferenceCardData && !props.onlineReferenceCardData) {
        return null;
    }

    const notesDataToShow = props.notesReferenceCardData.slice(0, numTeaserSlots);
    const onlineDataToShow =
        notesDataToShow.length < numTeaserSlots
            ? props.onlineReferenceCardData
                  .filter((online) => online.link)
                  .slice(0, numTeaserSlots - notesDataToShow.length)
            : [];

    const copyReferencesToClipboard = () => {
        navigator.clipboard.writeText(
            formatReferencesAsMarkdown(props.notesReferenceCardData, props.onlineReferenceCardData),
        );
        setCopyReferencesSuccess(true);
    };

    return (
        <Sheet>
            <SheetTrigger className="text-balance w-auto justify-start overflow-hidden break-words p-0 bg-transparent border-none text-gray-400 align-middle items-center m-0 inline-flex">
                {notesDataToShow.map((note, index) => {
                    return (
                        <NotesContextReferenceCard
                            showFullContent={false}
                            {...note}
                            key={`${note.title}-${index}`}
                        />
                    );
                })}
                {onlineDataToShow.map((online, index) => {
                    return (
                        <GenericOnlineReferenceCard
                            showFullContent={false}
                            {...online}
                            key={`${online.title}-${index}`}
                        />
                    );
                })}
                <ArrowRight className="m-0" />
            </SheetTrigger>
            <SheetContent className="overflow-y-scroll">
                <SheetHeader>
                    <SheetTitle>References</SheetTitle>
                    <SheetDescription>View all references for this response</SheetDescription>
                    <Button variant="outline" onClick={copyReferencesToClipboard} className="mt-4">
                        {copyReferencesSuccess ? (
                            <>
                                <Check className="mr-2 text-green-500" />
                                Copied!
                            </>
                        ) : (
                            <>
                                <Clipboard className="mr-2" />
                                Copy References
                            </>
                        )}
                    </Button>
                </SheetHeader>
                <div className="flex flex-wrap gap-2 w-auto mt-2">
                    {props.notesReferenceCardData.map((note, index) => {
                        return (
                            <NotesContextReferenceCard
                                showFullContent={true}
                                {...note}
                                key={`${note.title}-${index}`}
                            />
                        );
                    })}
                    {props.onlineReferenceCardData.map((online, index) => {
                        return (
                            <GenericOnlineReferenceCard
                                showFullContent={true}
                                {...online}
                                key={`${online.title}-${index}`}
                            />
                        );
                    })}
                </div>
            </SheetContent>
        </Sheet>
    );
}
