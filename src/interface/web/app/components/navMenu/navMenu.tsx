"use client";

import Link from "next/link";
import { useAuthenticatedData } from "@/app/common/auth";
import { useEffect } from "react";
import { Avatar, AvatarImage, AvatarFallback } from "@/components/ui/avatar";

import {
    DropdownMenu,
    DropdownMenuContent,
    DropdownMenuItem,
    DropdownMenuSeparator,
    DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Moon, Sun, UserCircle, Question, Code, BuildingOffice } from "@phosphor-icons/react";
import { useIsDarkMode } from "@/app/common/utils";
import { SidebarMenu, SidebarMenuButton, SidebarMenuItem } from "@/components/ui/sidebar";
import { ChevronUp } from "lucide-react";

function VersionBadge({ version }: { version: string }) {
    return (
        <div className="flex flex-row items-center">
            <div className="w-3 h-3 rounded-full bg-green-500 mr-1"></div>
            <p className="text-xs">{version}</p>
        </div>
    );
}

interface NavMenuProps {
    sideBarIsOpen: boolean;
}

export default function FooterMenu({ sideBarIsOpen }: NavMenuProps) {
    const {
        data: userData,
        error: authenticationError,
        isLoading: authenticationLoading,
    } = useAuthenticatedData();
    const [darkMode, setDarkMode] = useIsDarkMode();

    const menuItems = [
        {
            title: "OfferAgent Setup",
            icon: <Question className="w-6 h-6" />,
            link: "/settings",
        },
        {
            title: "Conversations",
            icon: <Code className="w-6 h-6" />,
            link: "/",
        },
        {
            title: "Agents",
            icon: <BuildingOffice className="w-6 h-6" />,
            link: "/agents",
        },
    ];

    return (
        <SidebarMenu className="border-none p-0 m-0">
            <SidebarMenuItem className="p-0 m-0">
                <DropdownMenu>
                    <DropdownMenuTrigger asChild>
                        <SidebarMenuButton className="p-0 m-0 rounded-lg" asChild>
                            {userData ? (
                                <span className="flex items-center gap-2">
                                    <Avatar
                                        className={`${sideBarIsOpen ? "h-8 w-8" : "h-6 w-6"} border-2 border-stone-700 dark:border-stone-300`}
                                    >
                                        <AvatarImage src={userData.photo} alt="user profile" />
                                        <AvatarFallback className="bg-transparent hover:bg-muted">
                                            {userData.username[0].toUpperCase()}
                                        </AvatarFallback>
                                    </Avatar>
                                    {sideBarIsOpen && (
                                        <>
                                            <p>{userData?.username}</p>
                                            <ChevronUp className="w-6 h-6 ml-auto" />
                                        </>
                                    )}
                                </span>
                            ) : (
                                <UserCircle className="w-10 h-10" />
                            )}
                        </SidebarMenuButton>
                    </DropdownMenuTrigger>
                    <DropdownMenuContent align="end" className="rounded-xl gap-2">
                        <DropdownMenuItem className="w-full">
                            <div className="flex flex-col">
                                <p className="font-semibold">{userData?.email}</p>
                                {userData?.khoj_version && (
                                    <VersionBadge version={userData?.khoj_version} />
                                )}
                            </div>
                        </DropdownMenuItem>
                        <DropdownMenuSeparator className="dark:bg-white height-[2px] bg-black" />
                        <DropdownMenuItem
                            onClick={() => setDarkMode(!darkMode)}
                            className="w-full hover:cursor-pointer"
                        >
                            <div className="flex flex-rows">
                                {darkMode ? (
                                    <Sun className="w-6 h-6" />
                                ) : (
                                    <Moon className="w-6 h-6" />
                                )}
                                <p className="ml-3 font-semibold">
                                    {darkMode ? "Light Mode" : "Dark Mode"}
                                </p>
                            </div>
                        </DropdownMenuItem>
                        {menuItems.map((menuItem, index) => (
                            <DropdownMenuItem key={index}>
                                <Link href={menuItem.link} className="no-underline w-full">
                                    <div className="flex flex-rows">
                                        {menuItem.icon}
                                        <p className="ml-3 font-semibold">{menuItem.title}</p>
                                    </div>
                                </Link>
                            </DropdownMenuItem>
                        ))}
                    </DropdownMenuContent>
                </DropdownMenu>
            </SidebarMenuItem>
        </SidebarMenu>
    );
}
