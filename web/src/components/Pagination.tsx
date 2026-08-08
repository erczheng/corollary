interface PaginationProps {
  page: number
  pageCount: number
  onChange: (page: number) => void
}

export function Pagination({ page, pageCount, onChange }: PaginationProps) {
  if (pageCount <= 1) return null

  return (
    <div className="flex items-center justify-between border-t border-outline/10 px-4 py-3 text-label-md text-on-surface-variant">
      <span>
        Page {page} of {pageCount}
      </span>
      <div className="flex gap-2">
        <button
          type="button"
          disabled={page <= 1}
          onClick={() => onChange(page - 1)}
          className="rounded border border-outline px-3 py-1 transition-colors duration-base ease-standard hover:bg-surface-container-low disabled:pointer-events-none disabled:opacity-40"
        >
          Prev
        </button>
        <button
          type="button"
          disabled={page >= pageCount}
          onClick={() => onChange(page + 1)}
          className="rounded border border-outline px-3 py-1 transition-colors duration-base ease-standard hover:bg-surface-container-low disabled:pointer-events-none disabled:opacity-40"
        >
          Next
        </button>
      </div>
    </div>
  )
}
