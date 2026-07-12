import React from "react";

import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import { Button } from "@/components/ui/button";

interface ProfileCardProps {
    name: string;
    avatar: JSX.Element;
    description?: string; // Optional description field
}

const AgentProfileCard: React.FC<ProfileCardProps> = ({ name, avatar, description }) => {
    return (
        <div className="relative group flex">
            <TooltipProvider>
                <Tooltip delayDuration={0}>
                    <TooltipTrigger asChild>
                        <Button variant="ghost" className="flex items-center justify-center">
                            {avatar}
                            <div>{name}</div>
                        </Button>
                    </TooltipTrigger>
                    <TooltipContent>
                        <div className="w-80 h-30">
                            <div className="mt-1 ml-2 flex items-center justify-start gap-2">
                                {avatar}
                                <div className="mr-2 mt-1 text-sm font-semibold text-gray-800">
                                    {name}
                                </div>
                            </div>
                            {description && (
                                <p className="mt-2 ml-6 text-sm text-gray-600 line-clamp-2">
                                    {description || "An OfferAgent agent"}
                                </p>
                            )}
                        </div>
                    </TooltipContent>
                </Tooltip>
            </TooltipProvider>
        </div>
    );
};

export default AgentProfileCard;
