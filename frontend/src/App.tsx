import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import {
  Bot,
  ChevronDown,
  FileText,
  LogOut,
  Moon,
  PlayCircle,
  Settings,
  SlidersHorizontal,
  Sun,
  UserRound,
  Users,
} from "lucide-react"
import {
  Link,
  Navigate,
  Outlet,
  Route,
  Routes,
  useLocation,
  useNavigate,
} from "react-router-dom"

import {
  AccountsPage,
  AccountWorkspacePage,
  NewAccountPage,
} from "@/pages/accounts"
import { LoginPage } from "@/pages/login"
import { LogsPage } from "@/pages/logs"
import {
  PresetsPage,
  PresetWorkspacePage,
  NewPresetPage,
} from "@/pages/presets"
import { RuntimePage } from "@/pages/runtime"
import { SettingsPage } from "@/pages/settings"
import { Button } from "@/components/ui/button"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import {
  NavigationMenu,
  NavigationMenuItem,
  NavigationMenuLink,
  NavigationMenuList,
} from "@/components/ui/navigation-menu"
import { Spinner } from "@/components/ui/spinner"
import { useTheme } from "@/components/theme-provider"
import { api, mutationError } from "@/lib/api"
import type { SessionData } from "@/types"

const navItems = [
  { to: "/", label: "Runtime", icon: PlayCircle },
  { to: "/accounts", label: "Accounts", icon: Users },
  { to: "/presets", label: "Presets", icon: SlidersHorizontal },
  { to: "/logs", label: "Logs", icon: FileText },
  { to: "/settings", label: "Settings", icon: Settings },
]

function PrimaryNavigation({ mobile = false }: { mobile?: boolean }) {
  const location = useLocation()
  const current = (to: string) =>
    to === "/" ? location.pathname === "/" : location.pathname.startsWith(to)

  return (
    <NavigationMenu className={mobile ? "w-full max-w-none" : undefined}>
      <NavigationMenuList
        className={mobile ? "grid w-full grid-cols-5 gap-0.5" : undefined}
      >
        {navItems.map((item) => (
          <NavigationMenuItem
            key={item.to}
            className={mobile ? "min-w-0" : undefined}
          >
            <NavigationMenuLink
              render={<Link to={item.to} />}
              data-active={current(item.to) || undefined}
              className={
                mobile
                  ? "min-h-12 min-w-0 flex-col justify-center gap-1 rounded-lg px-0.5 py-1 text-[0.65rem] leading-none"
                  : undefined
              }
            >
              <item.icon className={mobile ? "size-4 shrink-0" : undefined} />
              <span className="truncate">{item.label}</span>
            </NavigationMenuLink>
          </NavigationMenuItem>
        ))}
      </NavigationMenuList>
    </NavigationMenu>
  )
}

function ThemeToggle() {
  const { theme, setTheme } = useTheme()
  const dark = theme === "dark"
  return (
    <Button
      variant="ghost"
      size="icon-lg"
      className="min-h-11 min-w-11 md:min-h-9 md:min-w-9"
      aria-label={`Switch to ${dark ? "light" : "dark"} theme`}
      onClick={() => setTheme(dark ? "light" : "dark")}
    >
      {dark ? <Sun /> : <Moon />}
    </Button>
  )
}

function AppShell({ session }: { session: SessionData }) {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const logout = useMutation({
    mutationFn: () =>
      api<SessionData>("/session/logout", { method: "POST", json: {} }),
    onSuccess: async () => {
      await queryClient.invalidateQueries()
      navigate("/login", { replace: true })
    },
    onError: mutationError,
  })
  return (
    <div className="min-h-dvh bg-background">
      <header className="sticky top-0 z-40 border-b bg-background/95 backdrop-blur supports-backdrop-filter:bg-background/80">
        <div className="mx-auto flex h-14 max-w-[1600px] items-center gap-3 px-3 sm:px-5 lg:px-8">
          <Link
            to="/"
            className="flex min-h-11 min-w-11 items-center gap-2 rounded-lg font-medium focus-visible:ring-3 focus-visible:ring-ring/50 focus-visible:outline-none"
          >
            <span className="flex size-8 shrink-0 items-center justify-center rounded-lg bg-primary text-primary-foreground">
              <Bot className="size-4" />
            </span>
            <span className="hidden whitespace-nowrap sm:inline">
              Twitch Farm
            </span>
          </Link>

          <div className="hidden md:block">
            <PrimaryNavigation />
          </div>

          <div className="ml-auto flex items-center gap-1">
            <ThemeToggle />
            <DropdownMenu>
              <DropdownMenuTrigger
                render={
                  <Button
                    variant="ghost"
                    className="min-h-11 min-w-11 px-0 sm:px-2.5 md:min-h-8"
                    aria-label={`Operator menu for ${session.user?.username}`}
                  />
                }
              >
                <UserRound className="sm:hidden" />
                <span className="hidden max-w-32 truncate sm:inline">
                  {session.user?.username}
                </span>
                <ChevronDown className="hidden sm:block" />
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end">
                <DropdownMenuGroup>
                  <DropdownMenuLabel>Operator</DropdownMenuLabel>
                  <DropdownMenuSeparator />
                  <DropdownMenuItem onClick={() => logout.mutate()}>
                    <LogOut /> Log out
                  </DropdownMenuItem>
                </DropdownMenuGroup>
              </DropdownMenuContent>
            </DropdownMenu>
          </div>
        </div>
        <div className="px-2 pb-2 md:hidden">
          <PrimaryNavigation mobile />
        </div>
      </header>
      <main className="mx-auto grid max-w-[1600px] min-w-0 grid-cols-[minmax(0,1fr)] gap-6 px-3 py-5 sm:px-5 sm:py-7 lg:px-8">
        <Outlet />
      </main>
    </div>
  )
}

function SessionRouter() {
  const location = useLocation()
  const session = useQuery({
    queryKey: ["session"],
    queryFn: () => api<SessionData>("/session"),
    staleTime: 30_000,
    retry: false,
  })

  if (session.isLoading) {
    return (
      <div
        className="grid min-h-dvh place-items-center"
        aria-label="Loading session"
      >
        <Spinner className="size-6" />
      </div>
    )
  }
  if (session.isError) {
    return (
      <div className="grid min-h-dvh place-items-center p-6 text-sm text-muted-foreground">
        Unable to reach the control room.
      </div>
    )
  }
  if (!session.data?.authenticated) {
    return location.pathname === "/login" ? (
      <LoginPage />
    ) : (
      <Navigate to="/login" replace />
    )
  }
  if (location.pathname === "/login") return <Navigate to="/" replace />

  return (
    <Routes>
      <Route element={<AppShell session={session.data} />}>
        <Route index element={<RuntimePage />} />
        <Route path="accounts" element={<AccountsPage />} />
        <Route path="accounts/new" element={<NewAccountPage />} />
        <Route path="accounts/:id" element={<AccountWorkspacePage />} />
        <Route path="presets" element={<PresetsPage />} />
        <Route path="presets/new" element={<NewPresetPage />} />
        <Route path="presets/:id" element={<PresetWorkspacePage />} />
        <Route path="logs" element={<LogsPage />} />
        <Route path="settings" element={<SettingsPage />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  )
}

export default function App() {
  return <SessionRouter />
}
