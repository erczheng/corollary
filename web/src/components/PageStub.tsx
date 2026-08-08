interface PageStubProps {
  title: string
  description: string
}

/** Placeholder body for pages not yet built. Phase 1 is shell only — real
 * content lands per PRD.md §8 as each phase connects real data. */
export function PageStub({ title, description }: PageStubProps) {
  return (
    <div className="mx-auto max-w-[1140px] px-4 py-20 lg:px-8">
      <h1 className="text-headline-md text-on-surface">{title}</h1>
      <p className="mt-3 max-w-prose text-body-md text-on-surface-variant">
        {description}
      </p>
    </div>
  )
}
