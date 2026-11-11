"use client";

import * as React from "react";
import * as ScrollAreaPrimitive from "@radix-ui/react-scroll-area";

import { cn } from "@/lib/utils";

interface ScrollAreaProps extends React.ComponentPropsWithoutRef<
  typeof ScrollAreaPrimitive.Root
> {
  viewportRef?: React.Ref<HTMLDivElement>;
}

const ScrollArea = React.forwardRef<
  React.ElementRef<typeof ScrollAreaPrimitive.Root>,
  ScrollAreaProps
>(({ className, children, viewportRef, ...props }, ref) => (
  <ScrollAreaPrimitive.Root
    ref={ref}
    className={cn(
      // Radix wraps the viewport content in an internal `display: table` div so
      // it can measure horizontal overflow. A table grows to the widest
      // unbreakable descendant (long URLs, hashes, or pasted text whose spaces
      // are non-breaking), which widens the whole page and pushes fixed controls
      // — e.g. the composer's stop button — outside the viewport. These scroll
      // areas only scroll vertically, so keep the wrapper a block and let long
      // content wrap instead.
      "relative overflow-hidden [&>[data-radix-scroll-area-viewport]>div]:!block",
      className,
    )}
    {...props}
  >
    <ScrollAreaPrimitive.Viewport
      ref={viewportRef}
      className="size-full rounded-[inherit]"
    >
      {children}
    </ScrollAreaPrimitive.Viewport>
    <ScrollBar />
    <ScrollAreaPrimitive.Corner />
  </ScrollAreaPrimitive.Root>
));
ScrollArea.displayName = ScrollAreaPrimitive.Root.displayName;

const ScrollBar = React.forwardRef<
  React.ElementRef<typeof ScrollAreaPrimitive.ScrollAreaScrollbar>,
  React.ComponentPropsWithoutRef<typeof ScrollAreaPrimitive.ScrollAreaScrollbar>
>(({ className, orientation = "vertical", ...props }, ref) => (
  <ScrollAreaPrimitive.ScrollAreaScrollbar
    ref={ref}
    orientation={orientation}
    className={cn(
      "flex touch-none select-none transition-colors",
      orientation === "vertical" &&
        "h-full w-1.5 border-l border-l-transparent p-[1px]",
      orientation === "horizontal" &&
        "h-2.5 flex-col border-t border-t-transparent p-[1px]",
      className,
    )}
    {...props}
  >
    <ScrollAreaPrimitive.ScrollAreaThumb className="relative flex-1 max-w-1 rounded-full bg-accent" />
  </ScrollAreaPrimitive.ScrollAreaScrollbar>
));
ScrollBar.displayName = ScrollAreaPrimitive.ScrollAreaScrollbar.displayName;

export { ScrollArea, ScrollBar };
