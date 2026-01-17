import UIKit
import SwiftUI
import StoreKit
import AuthenticationServices

@UIApplicationMain
class AppDelegate: UIResponder, UIApplicationDelegate {
    var window: UIWindow?

    func application(
        _ application: UIApplication,
        didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]?
    ) -> Bool {
        let tabBarAppearance = UITabBarAppearance()
        tabBarAppearance.configureWithOpaqueBackground()
        tabBarAppearance.backgroundColor = UIColor.black
        tabBarAppearance.shadowColor = UIColor(white: 0.15, alpha: 1.0)

        let itemAppearance = UITabBarItemAppearance()
        itemAppearance.normal.iconColor = UIColor(white: 0.55, alpha: 1.0)
        itemAppearance.normal.titleTextAttributes = [
            .foregroundColor: UIColor(white: 0.55, alpha: 1.0)
        ]
        itemAppearance.selected.iconColor = AppColors.accentUIColor
        itemAppearance.selected.titleTextAttributes = [
            .foregroundColor: AppColors.accentUIColor
        ]

        tabBarAppearance.stackedLayoutAppearance = itemAppearance
        tabBarAppearance.inlineLayoutAppearance = itemAppearance
        tabBarAppearance.compactInlineLayoutAppearance = itemAppearance

        UITabBar.appearance().standardAppearance = tabBarAppearance
        if #available(iOS 15.0, *) {
            UITabBar.appearance().scrollEdgeAppearance = tabBarAppearance
        }

        let navAppearance = UINavigationBarAppearance()
        navAppearance.configureWithOpaqueBackground()
        navAppearance.backgroundColor = UIColor.black
        navAppearance.titleTextAttributes = [.foregroundColor: UIColor.white]
        navAppearance.largeTitleTextAttributes = [.foregroundColor: UIColor.white]

        UINavigationBar.appearance().standardAppearance = navAppearance
        UINavigationBar.appearance().compactAppearance = navAppearance
        UINavigationBar.appearance().scrollEdgeAppearance = navAppearance

        return true
    }
}

class SceneDelegate: UIResponder, UIWindowSceneDelegate {
    var window: UIWindow?

    func scene(
        _ scene: UIScene,
        willConnectTo session: UISceneSession,
        options connectionOptions: UIScene.ConnectionOptions
    ) {
        guard let windowScene = scene as? UIWindowScene else { return }
        let window = UIWindow(windowScene: windowScene)
        window.rootViewController = UIHostingController(rootView: AppRootView())
        self.window = window
        window.makeKeyAndVisible()
    }
}

enum AppColors {
    static let background = Color(red: 0.02, green: 0.02, blue: 0.02)
    static let backgroundTop = Color(red: 0.03, green: 0.04, blue: 0.1)
    static let backgroundBottom = Color(red: 0.01, green: 0.01, blue: 0.03)
    static let card = Color(red: 0.09, green: 0.09, blue: 0.09)
    static let cardSoft = Color(red: 0.12, green: 0.12, blue: 0.12)
    static let cardHighlight = Color(red: 0.13, green: 0.14, blue: 0.18)
    static let cardBorder = Color(red: 0.18, green: 0.2, blue: 0.28)
    static let accent = Color(red: 0.82, green: 0.68, blue: 0.2)
    static let accentStrong = Color(red: 0.9, green: 0.75, blue: 0.2)
    static let textPrimary = Color.white
    static let textSecondary = Color(red: 0.72, green: 0.72, blue: 0.72)
    static let textMuted = Color(red: 0.55, green: 0.55, blue: 0.55)
    static let success = Color(red: 0.3, green: 0.8, blue: 0.4)
    static let danger = Color(red: 0.92, green: 0.33, blue: 0.33)
    static let divider = Color(red: 0.18, green: 0.18, blue: 0.18)

    static var accentUIColor: UIColor {
        UIColor(red: 0.82, green: 0.68, blue: 0.2, alpha: 1.0)
    }
}

enum AppConfig {
    static var baseURL: URL {
        if let raw = Bundle.main.object(forInfoDictionaryKey: "API_BASE_URL") as? String,
           let url = URL(string: raw) {
            return url
        }
        return URL(string: "https://api.ai-insider-trading.com")!
    }
}

struct AppScreenBackground: View {
    var body: some View {
        LinearGradient(
            colors: [AppColors.backgroundTop, AppColors.backgroundBottom],
            startPoint: .topLeading,
            endPoint: .bottomTrailing
        )
        .overlay(
            RadialGradient(
                colors: [AppColors.accent.opacity(0.08), Color.clear],
                center: .topLeading,
                startRadius: 0,
                endRadius: 420
            )
        )
        .ignoresSafeArea()
    }
}

struct AppRootView: View {
    @StateObject private var session = AppSession()
    @StateObject private var subscription = SubscriptionManager()

    var body: some View {
        ZStack {
            AppScreenBackground()
            if session.isLoading {
                SplashView()
            } else if session.isAuthenticated {
                MainTabView()
                    .environmentObject(session)
                    .environmentObject(subscription)
            } else {
                LoginView()
                    .environmentObject(session)
            }
        }
        .task {
            await session.refresh()
            await subscription.refresh()
        }
    }
}

struct SplashView: View {
    var body: some View {
        VStack(spacing: 16) {
            ZStack {
                Circle()
                    .fill(AppColors.cardSoft)
                    .frame(width: 72, height: 72)
                Image(systemName: "pawprint.fill")
                    .font(.system(size: 32, weight: .bold))
                    .foregroundColor(AppColors.accent)
            }
            Text("Wolf of Washington")
                .font(.system(size: 22, weight: .semibold))
                .foregroundColor(AppColors.textPrimary)
            Text("Loading market signals...")
                .font(.system(size: 14))
                .foregroundColor(AppColors.textMuted)
        }
    }
}

@MainActor
final class AppSession: ObservableObject {
    @Published var isLoading = true
    @Published var isAuthenticated = false
    @Published var user: String?
    @Published var authDisabled = false
    @Published var lastError: String?

    func refresh() async {
        isLoading = true
        defer { isLoading = false }
        do {
            let response: MeResponse = try await APIClient.shared.request("api/me")
            authDisabled = response.authDisabled
            user = response.user
            isAuthenticated = authDisabled || response.user != nil
        } catch {
            isAuthenticated = false
            lastError = "Unable to reach the API."
        }
    }

    func loginWithApple(identityToken: String, email: String?, fullName: String?) async -> Bool {
        lastError = nil
        do {
            let payload = AppleAuthRequest(identityToken: identityToken, email: email, fullName: fullName)
            let response: AppleAuthResponse = try await APIClient.shared.request(
                "api/auth/apple",
                method: "POST",
                body: payload
            )
            user = response.user
            authDisabled = response.authDisabled ?? false
            isAuthenticated = true
            return true
        } catch let error as APIError {
            lastError = error.message
            return false
        } catch {
            lastError = "Apple login failed."
            return false
        }
    }

    func logout() async {
        do {
            let _: BasicResponse = try await APIClient.shared.request(
                "api/logout",
                method: "POST"
            )
        } catch {
            // Ignore logout failures.
        }
        user = nil
        isAuthenticated = false
    }
}

@MainActor
final class SubscriptionManager: ObservableObject {
    @Published var products: [Product] = []
    @Published var isSubscribed = false
    @Published var isLoading = false
    @Published var errorMessage: String?

    private let productIDs = [
        "com.aiinsidertrading.pro.monthly",
        "com.aiinsidertrading.pro.yearly"
    ]

    func refresh() async {
        await loadProducts()
        await refreshEntitlements()
    }

    func loadProducts() async {
        isLoading = true
        defer { isLoading = false }
        do {
            products = try await Product.products(for: productIDs)
            errorMessage = nil
        } catch {
            errorMessage = "Unable to load subscriptions."
        }
    }

    func purchase(_ product: Product) async {
        isLoading = true
        errorMessage = nil
        defer { isLoading = false }
        do {
            let result = try await product.purchase()
            switch result {
            case .success(let verification):
                let transaction = try checkVerified(verification)
                await transaction.finish()
                await refreshEntitlements()
            case .userCancelled:
                break
            case .pending:
                errorMessage = "Purchase pending approval."
            @unknown default:
                errorMessage = "Purchase failed."
            }
        } catch {
            errorMessage = "Purchase failed."
        }
    }

    func restore() async {
        isLoading = true
        errorMessage = nil
        defer { isLoading = false }
        do {
            try await AppStore.sync()
            await refreshEntitlements()
        } catch {
            errorMessage = "Restore failed."
        }
    }

    func refreshEntitlements() async {
        var activeIDs: Set<String> = []
        for await result in Transaction.currentEntitlements {
            do {
                let transaction = try checkVerified(result)
                if productIDs.contains(transaction.productID) {
                    activeIDs.insert(transaction.productID)
                }
            } catch {
                continue
            }
        }
        isSubscribed = !activeIDs.isEmpty
    }

    private func checkVerified<T>(_ result: VerificationResult<T>) throws -> T {
        switch result {
        case .verified(let safe):
            return safe
        case .unverified:
            throw APIError(message: "Unverified transaction.")
        }
    }
}

final class APIClient {
    static let shared = APIClient()

    private let session: URLSession
    private let baseURL: URL
    private let decoder: JSONDecoder
    private let encoder: JSONEncoder

    private init() {
        let config = URLSessionConfiguration.default
        config.httpCookieStorage = .shared
        config.httpShouldSetCookies = true
        session = URLSession(configuration: config)
        baseURL = AppConfig.baseURL

        decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase

        encoder = JSONEncoder()
        encoder.keyEncodingStrategy = .convertToSnakeCase
    }

    func request<T: Decodable>(
        _ path: String,
        method: String = "GET",
        query: [String: String?] = [:],
        body: Encodable? = nil
    ) async throws -> T {
        let url = try buildURL(path: path, query: query)
        var request = URLRequest(url: url)
        request.httpMethod = method
        request.setValue("application/json", forHTTPHeaderField: "Accept")

        if let body = body {
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = try encoder.encode(AnyEncodable(body))
        }

        let (data, response) = try await session.data(for: request)
        guard let httpResponse = response as? HTTPURLResponse else {
            throw APIError(message: "Invalid server response.")
        }

        if (200..<300).contains(httpResponse.statusCode) {
            return try decoder.decode(T.self, from: data)
        }

        if let apiError = try? decoder.decode(APIErrorPayload.self, from: data) {
            throw APIError(message: apiError.detail)
        }

        throw APIError(message: "Request failed with status \(httpResponse.statusCode).")
    }

    private func buildURL(path: String, query: [String: String?]) throws -> URL {
        let trimmed = path.hasPrefix("/") ? String(path.dropFirst()) : path
        let url = baseURL.appendingPathComponent(trimmed)
        guard var components = URLComponents(url: url, resolvingAgainstBaseURL: false) else {
            throw APIError(message: "Invalid URL.")
        }
        let items = query.compactMap { key, value -> URLQueryItem? in
            guard let value = value, !value.isEmpty else { return nil }
            return URLQueryItem(name: key, value: value)
        }
        if !items.isEmpty {
            components.queryItems = items
        }
        if let finalURL = components.url {
            return finalURL
        }
        throw APIError(message: "Invalid URL.")
    }
}

struct APIError: Error {
    let message: String
}

struct APIErrorPayload: Decodable {
    let detail: String
}

struct AnyEncodable: Encodable {
    private let encodeBlock: (Encoder) throws -> Void

    init(_ value: Encodable) {
        self.encodeBlock = value.encode
    }

    func encode(to encoder: Encoder) throws {
        try encodeBlock(encoder)
    }
}

struct BasicResponse: Decodable {
    let ok: Bool
}

struct MeResponse: Decodable {
    let authDisabled: Bool
    let user: String?
}

struct AppleAuthRequest: Encodable {
    let identityToken: String
    let email: String?
    let fullName: String?
}

struct AppleAuthResponse: Decodable {
    let ok: Bool
    let user: String?
    let authDisabled: Bool?
}

struct DashboardResponse: Decodable {
    let stats: DashboardStats
    let latestTrades: [Trade]
}

struct DashboardStats: Decodable {
    let total24h: Int
    let topTicker: String?
    let latestForm: String?
}

struct TradesResponse: Decodable {
    let items: [Trade]
    let total: Int
    let limit: Int
    let offset: Int
}

struct Trade: Decodable, Identifiable {
    let id: Int
    let externalId: String
    let ticker: String?
    let companyName: String?
    let personName: String?
    let personSlug: String?
    let transactionType: String?
    let transactionLabel: String?
    let form: String?
    let transactionDate: String?
    let filedAt: String?
    let amountUsdLow: Int?
    let amountUsdHigh: Int?
    let amountUsd: Double?
    let shares: Int?
    let priceUsd: String?
    let priceUsdValue: Double?
    let url: String?
    let score: Int?
    let delayDays: Int?
    let isBuy: Bool
    let isSell: Bool

    var displayTicker: String {
        ticker ?? "-"
    }

    var displayCompany: String {
        companyName ?? "Unknown company"
    }

    var displayPerson: String {
        personName ?? personSlug ?? "Unknown"
    }

    var displayAmount: String {
        if let amountUsd = amountUsd {
            return "$" + Trade.formatNumber(amountUsd)
        }
        if let low = amountUsdLow, let high = amountUsdHigh, low != high {
            return "$\(low) - $\(high)"
        }
        if let low = amountUsdLow {
            return "$\(low)"
        }
        if let high = amountUsdHigh {
            return "$\(high)"
        }
        return "-"
    }

    var displayDelay: String? {
        guard let delayDays = delayDays, delayDays > 0 else { return nil }
        return "+\(delayDays)d delay"
    }

    var buySellLabel: String {
        if isBuy { return "BUY" }
        if isSell { return "SELL" }
        return transactionLabel ?? (transactionType ?? "-")
    }

    private static let numberFormatter: NumberFormatter = {
        let formatter = NumberFormatter()
        formatter.numberStyle = .decimal
        formatter.maximumFractionDigits = 2
        return formatter
    }()

    private static func formatNumber(_ value: Double) -> String {
        numberFormatter.string(from: NSNumber(value: value)) ?? String(format: "%.2f", value)
    }
}

struct NewsCard: Identifiable {
    let id = UUID()
    let tag: String
    let title: String
    let subtitle: String
    let dateText: String
}

struct SearchResponse: Decodable {
    let query: String
    let tickers: [TickerResult]
    let people: [PeopleResult]
    let watchlistTickers: [String]
    let watchlistPeople: [String]
}

struct TickerResult: Decodable, Identifiable {
    let ticker: String
    let companyName: String?
    let count: Int

    var id: String { ticker }
}

struct PeopleResult: Decodable, Identifiable {
    let slug: String
    let name: String
    let count: Int

    var id: String { slug }
}

struct PeopleResponse: Decodable {
    let items: [PersonListItem]
    let page: Int
    let pageSize: Int
    let total: Int
    let totalPages: Int
}

struct PersonListItem: Decodable, Identifiable {
    let slug: String
    let name: String
    let count: Int
    let watchlisted: Bool

    var id: String { slug }
}

struct FormCount: Decodable, Identifiable {
    let prefix: String
    let label: String
    let count: Int

    var id: String { prefix }
}

struct PersonDetailResponse: Decodable {
    let slug: String
    let name: String
    let total: Int
    let forms: [FormCount]
    let summary: String?
    let summaryUpdatedAt: String?
    let watchlisted: Bool
    let watchlistItemId: Int?
    let trades: [Trade]
}

struct CompanyDetailResponse: Decodable {
    let ticker: String
    let companyName: String?
    let total: Int
    let forms: [FormCount]
    let latestPrice: Double?
    let latestPriceDate: String?
    let watchlisted: Bool
    let watchlistItemId: Int?
    let trades: [Trade]
}

struct WatchlistResponse: Decodable {
    let items: [WatchlistItem]
    let trades: [Trade]
}

struct WatchlistItem: Decodable, Identifiable {
    let id: Int
    let kind: String
    let value: String
    let label: String?
    let createdAt: String?
}

struct WatchlistAddRequest: Encodable {
    let kind: String
    let value: String
    let label: String?
}

struct PortfolioResponse: Decodable {
    let transactions: [PortfolioTransaction]
    let imports: [PortfolioImport]
    let connections: [BrokerConnection]
    let brokers: [BrokerCatalogItem]
}

struct PortfolioTransaction: Decodable, Identifiable {
    let id: Int
    let externalId: String
    let broker: String?
    let brokerLabel: String?
    let account: String?
    let activityType: String?
    let symbol: String?
    let name: String?
    let tradeDate: String?
    let settlementDate: String?
    let quantity: Double?
    let price: Double?
    let fees: Double?
    let amount: Double?
    let currency: String?
    let notes: String?
    let createdAt: String?
}

struct PortfolioImport: Decodable, Identifiable {
    let id: Int
    let source: String
    let broker: String?
    let brokerLabel: String?
    let status: String
    let fileName: String?
    let fileSizeBytes: Int?
    let inserted: Int?
    let updated: Int?
    let errorCount: Int?
    let message: String?
    let createdAt: String?
}

struct BrokerConnection: Decodable, Identifiable {
    let broker: String
    let brokerLabel: String?
    let account: String?
    let status: String
    let lastSyncedAt: String?
    let errorMessage: String?

    var id: String { "\(broker)-\(account ?? "default")" }
}

struct BrokerCatalogItem: Decodable, Identifiable {
    let slug: String
    let label: String

    var id: String { slug }
}

struct PricesResponse: Decodable {
    let ticker: String
    let range: String
    let ranges: [PriceRangeOption]
    let resolvedSymbol: String?
    let labels: [String]
    let values: [Double]
    let stats: PriceStats
    let error: String?
}

struct PriceRangeOption: Decodable, Identifiable {
    let code: String
    let label: String

    var id: String { code }
}

struct PriceStats: Decodable {
    let firstDate: String?
    let lastDate: String?
    let firstClose: Double?
    let lastClose: Double?
    let changeAbs: Double?
    let changePct: Double?
    let changePositive: Bool?
}

struct SettingsResponse: Decodable {
    let appName: String
    let publicBaseUrl: String
    let dbKind: String
    let ingestConfigured: Bool
    let authDisabled: Bool
    let webUiEnabled: Bool
}

enum AppTab: String, CaseIterable {
    case dashboard
    case news
    case trades
    case briefing
    case account
}

struct MainTabView: View {
    @State private var selection: AppTab = .dashboard
    @EnvironmentObject private var subscription: SubscriptionManager

    var body: some View {
        TabView(selection: $selection) {
            NavigationView {
                SubscriptionGateView(requiresSubscription: false) {
                    DashboardView()
                }
            }
            .tabItem {
                Label("Dashboard", systemImage: "house.fill")
            }
            .tag(AppTab.dashboard)

            NavigationView {
                SubscriptionGateView(requiresSubscription: true) {
                    NewsView()
                }
            }
            .tabItem {
                Label("Laatste nieuws", systemImage: "newspaper.fill")
            }
            .tag(AppTab.news)

            NavigationView {
                SubscriptionGateView(requiresSubscription: true) {
                    TradesView()
                }
            }
            .tabItem {
                Label("Transacties", systemImage: "chart.line.uptrend.xyaxis")
            }
            .tag(AppTab.trades)

            NavigationView {
                SubscriptionGateView(requiresSubscription: true) {
                    BriefingView()
                }
            }
            .tabItem {
                Label("Briefing", systemImage: "clock.arrow.circlepath")
            }
            .tag(AppTab.briefing)

            NavigationView {
                SubscriptionGateView(requiresSubscription: false) {
                    AccountView()
                }
            }
            .tabItem {
                Label("Account", systemImage: "person.crop.circle")
            }
            .tag(AppTab.account)
        }
        .accentColor(AppColors.accent)
    }
}

struct SubscriptionGateView<Content: View>: View {
    @EnvironmentObject private var subscription: SubscriptionManager
    let requiresSubscription: Bool
    let content: () -> Content

    init(requiresSubscription: Bool, @ViewBuilder content: @escaping () -> Content) {
        self.requiresSubscription = requiresSubscription
        self.content = content
    }

    var body: some View {
        if !requiresSubscription || subscription.isSubscribed {
            content()
        } else {
            PaywallView()
        }
    }
}

struct PaywallView: View {
    @EnvironmentObject private var subscription: SubscriptionManager

    var body: some View {
        ZStack {
            AppScreenBackground()
            ScrollView {
                PaywallContent(inline: false)
                    .padding(.horizontal, 20)
                    .padding(.vertical, 24)
            }
        }
        .navigationBarHidden(true)
        .task {
            if subscription.products.isEmpty {
                await subscription.loadProducts()
            }
        }
    }
}

struct PaywallContent: View {
    @EnvironmentObject private var subscription: SubscriptionManager
    let inline: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: inline ? 16 : 22) {
            if !inline {
                VStack(alignment: .leading, spacing: 10) {
                    Text("Upgrade to Pro")
                        .font(.system(size: 28, weight: .bold))
                        .foregroundColor(AppColors.textPrimary)
                    Text("Unlock all trades, real-time alerts, and portfolio insights.")
                        .font(.system(size: 14))
                        .foregroundColor(AppColors.textSecondary)
                }
            } else {
                Text("Nu abonneren")
                    .font(.system(size: 18, weight: .semibold))
                    .foregroundColor(AppColors.textPrimary)
            }

            VStack(alignment: .leading, spacing: 8) {
                SubscriptionFeatureRow(text: "Full access to politician trades")
                SubscriptionFeatureRow(text: "Real-time notifications")
                SubscriptionFeatureRow(text: "Performance analytics")
                SubscriptionFeatureRow(text: "Portfolio insights")
            }

            if subscription.isLoading && subscription.products.isEmpty {
                LoadingCard()
            } else if subscription.products.isEmpty {
                Text("Subscriptions are not configured yet.")
                    .font(.system(size: 12))
                    .foregroundColor(AppColors.textMuted)
            } else {
                VStack(spacing: 12) {
                    ForEach(subscription.products, id: \.id) { product in
                        SubscriptionProductCard(product: product)
                    }
                }
            }

            if let error = subscription.errorMessage {
                Text(error)
                    .font(.system(size: 12))
                    .foregroundColor(AppColors.danger)
            }

            Button {
                Task { await subscription.restore() }
            } label: {
                Text("Restore purchases")
                    .font(.system(size: 13, weight: .semibold))
                    .foregroundColor(AppColors.textSecondary)
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 12)
                    .background(Color.clear)
                    .overlay(
                        RoundedRectangle(cornerRadius: 12)
                            .stroke(AppColors.cardBorder, lineWidth: 1)
                    )
            }
        }
    }
}

struct SubscriptionProductCard: View {
    @EnvironmentObject private var subscription: SubscriptionManager
    let product: Product

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                VStack(alignment: .leading, spacing: 4) {
                    Text(product.displayName)
                        .font(.system(size: 16, weight: .semibold))
                        .foregroundColor(AppColors.textPrimary)
                    if let periodText = periodText {
                        Text(periodText)
                            .font(.system(size: 12))
                            .foregroundColor(AppColors.textMuted)
                    }
                }
                Spacer()
                Text(product.displayPrice)
                    .font(.system(size: 18, weight: .bold))
                    .foregroundColor(AppColors.accent)
            }

            Button {
                Task { await subscription.purchase(product) }
            } label: {
                Text("Subscribe")
                    .font(.system(size: 13, weight: .semibold))
                    .foregroundColor(.black)
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 10)
                    .background(AppColors.accent)
                    .cornerRadius(12)
            }
        }
        .padding(14)
        .background(AppColors.card)
        .cornerRadius(16)
        .overlay(
            RoundedRectangle(cornerRadius: 16)
                .stroke(AppColors.cardBorder, lineWidth: 1)
        )
    }

    private var periodText: String? {
        guard let period = product.subscription?.subscriptionPeriod else { return nil }
        let unit: String
        switch period.unit {
        case .day:
            unit = "day"
        case .week:
            unit = "week"
        case .month:
            unit = "month"
        case .year:
            unit = "year"
        @unknown default:
            unit = "period"
        }
        let value = period.value
        if value == 1 {
            return "per \(unit)"
        }
        return "per \(value) \(unit)s"
    }
}

struct SubscriptionFeatureRow: View {
    let text: String

    var body: some View {
        HStack(spacing: 8) {
            Image(systemName: "checkmark.circle.fill")
                .foregroundColor(AppColors.accent)
            Text(text)
                .font(.system(size: 12))
                .foregroundColor(AppColors.textSecondary)
        }
    }
}

struct DashboardView: View {
    @State private var dashboard: DashboardResponse?
    @State private var isLoading = false
    @State private var errorMessage: String?

    var body: some View {
        ZStack {
            AppScreenBackground()
            ScrollView {
                VStack(alignment: .leading, spacing: 22) {
                    HStack(spacing: 12) {
                        AppLogoMark()
                        Spacer()
                        Image(systemName: "bell.fill")
                            .foregroundColor(AppColors.accent)
                            .padding(10)
                            .background(AppColors.cardSoft)
                            .cornerRadius(12)
                    }

                    VStack(alignment: .leading, spacing: 10) {
                        Text("Alternative market data, made usable.")
                            .font(.system(size: 30, weight: .bold))
                            .foregroundColor(AppColors.textPrimary)
                        Text("Track insider trades, congress trades, filings, and other signals in one fast dashboard.")
                            .font(.system(size: 14))
                            .foregroundColor(AppColors.textSecondary)
                    }

                    HStack(spacing: 12) {
                        NavigationLink(destination: TradesView()) {
                            AppActionButton(title: "Explore data", filled: true)
                        }
                        .buttonStyle(.plain)
                        NavigationLink(destination: WatchlistView()) {
                            AppActionButton(title: "Watchlist", filled: false)
                        }
                        .buttonStyle(.plain)
                    }

                    if let stats = dashboard?.stats {
                        HStack(spacing: 12) {
                            StatCard(title: "24h trades", value: "\(stats.total24h)")
                            StatCard(title: "Top ticker", value: stats.topTicker ?? "-")
                        }
                    }

                    AppCard {
                        VStack(alignment: .leading, spacing: 14) {
                            Text("Preview")
                                .font(.system(size: 13, weight: .semibold))
                                .foregroundColor(AppColors.textMuted)

                            PreviewRow(
                                title: "Latest Insider Trade",
                                value: previewValue(for: insiderTrade())
                            )
                            AppDivider()
                            PreviewRow(
                                title: "Latest Congress Trade",
                                value: previewValue(for: congressTrade())
                            )
                            AppDivider()
                            PreviewRow(title: "Watchlist", value: "Coming next")
                        }
                    }

                    HStack {
                        Text("Recente transacties")
                            .font(.system(size: 20, weight: .semibold))
                            .foregroundColor(AppColors.textPrimary)
                        Spacer()
                        TagPill(text: "3+ MAANDEN OUD", color: AppColors.danger)
                    }

                    if isLoading {
                        LoadingCard()
                    } else if let trades = dashboard?.latestTrades {
                        ForEach(trades.prefix(6)) { trade in
                            NavigationLink(destination: TradeDetailBridgeView(trade: trade)) {
                                TradeCardView(trade: trade)
                            }
                            .buttonStyle(.plain)
                        }
                    } else if let errorMessage = errorMessage {
                        ErrorCard(message: errorMessage)
                    }
                }
                .padding(.horizontal, 20)
                .padding(.vertical, 16)
            }
        }
        .navigationBarHidden(true)
        .task {
            await loadDashboard()
        }
        .refreshable {
            await loadDashboard()
        }
    }

    private func insiderTrade() -> Trade? {
        dashboard?.latestTrades.first { ($0.form ?? "").uppercased().contains("FORM 4") }
            ?? dashboard?.latestTrades.first
    }

    private func congressTrade() -> Trade? {
        dashboard?.latestTrades.first { ($0.form ?? "").uppercased().contains("CONGRESS") }
            ?? dashboard?.latestTrades.dropFirst().first
    }

    private func previewValue(for trade: Trade?) -> String {
        guard let trade = trade else { return "Coming soon" }
        return "\(trade.displayTicker) — \(trade.buySellLabel) — \(trade.displayAmount)"
    }

    private func loadDashboard() async {
        isLoading = true
        defer { isLoading = false }
        do {
            dashboard = try await APIClient.shared.request("api/dashboard")
            errorMessage = nil
        } catch {
            errorMessage = "Kon dashboard niet laden."
        }
    }
}

struct NewsView: View {
    @State private var items: [NewsCard] = []
    @State private var isLoading = false

    var body: some View {
        ZStack {
            AppScreenBackground()
            ScrollView {
                VStack(alignment: .leading, spacing: 18) {
                    Text("Laatste nieuws")
                        .font(.system(size: 24, weight: .bold))
                        .foregroundColor(AppColors.textPrimary)

                    if isLoading && items.isEmpty {
                        LoadingCard()
                    } else {
                        ForEach(items) { item in
                            NewsCardView(card: item)
                        }
                    }
                }
                .padding(.horizontal, 20)
                .padding(.vertical, 16)
            }
        }
        .navigationTitle("")
        .navigationBarHidden(true)
        .task {
            await loadNews()
        }
        .refreshable {
            await loadNews()
        }
    }

    private func loadNews() async {
        isLoading = true
        defer { isLoading = false }
        do {
            let response: TradesResponse = try await APIClient.shared.request(
                "api/trades",
                query: ["limit": "10"]
            )
            items = response.items.map { trade in
                NewsCard(
                    tag: "Trade Alert",
                    title: "\(trade.displayPerson) heeft een positie bijgewerkt in \(trade.displayCompany).",
                    subtitle: "\(trade.buySellLabel) \(trade.displayTicker)",
                    dateText: trade.transactionDate ?? "-"
                )
            }
        } catch {
            items = []
        }
    }
}

struct TradesView: View {
    @State private var trades: [Trade] = []
    @State private var query = ""
    @State private var selectedFilter: TradeFilter = .all
    @State private var isLoading = false

    var body: some View {
        ZStack {
            AppScreenBackground()
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    HStack(alignment: .center, spacing: 12) {
                        Text("Recente Transacties")
                            .font(.system(size: 28, weight: .bold))
                            .foregroundColor(AppColors.textPrimary)
                        Spacer()
                        TagPill(text: "3+ MAANDEN OUD", color: AppColors.danger)
                    }

                    SearchBar(text: $query, placeholder: "Zoek politici of aandelen") {
                        Task { await loadTrades() }
                    }

                    FilterPills(selected: $selectedFilter)

                    if isLoading {
                        LoadingCard()
                    }

                    ForEach(trades) { trade in
                        NavigationLink(destination: TradeDetailBridgeView(trade: trade)) {
                            TradeCardView(trade: trade)
                        }
                        .buttonStyle(.plain)
                    }
                }
                .padding(.horizontal, 20)
                .padding(.vertical, 16)
            }
        }
        .navigationBarHidden(true)
        .task {
            await loadTrades()
        }
        .refreshable {
            await loadTrades()
        }
        .onChange(of: selectedFilter) { _ in
            Task { await loadTrades() }
        }
    }

    private func loadTrades() async {
        isLoading = true
        defer { isLoading = false }

        var ticker: String? = nil
        var person: String? = nil
        let trimmed = query.trimmingCharacters(in: .whitespacesAndNewlines)
        if !trimmed.isEmpty {
            if trimmed.count <= 6, trimmed.range(of: "^[A-Za-z0-9._-]+$", options: .regularExpression) != nil {
                ticker = trimmed.uppercased()
            } else {
                person = trimmed
            }
        }

        let response: TradesResponse?
        do {
            response = try await APIClient.shared.request(
                "api/trades",
                query: [
                    "limit": "50",
                    "ticker": ticker,
                    "person": person,
                    "type": selectedFilter.apiValue
                ]
            )
        } catch {
            response = nil
        }
        trades = response?.items ?? []
    }
}

enum TradeFilter: String, CaseIterable {
    case all = "Alle transacties"
    case buy = "Aankopen"
    case sell = "Verkopen"

    var apiValue: String? {
        switch self {
        case .all:
            return nil
        case .buy:
            return "BUY"
        case .sell:
            return "SELL"
        }
    }
}

struct BriefingView: View {
    @State private var watchlist: WatchlistResponse?
    @State private var isLoading = false

    var body: some View {
        ZStack {
            AppScreenBackground()
            ScrollView {
                VStack(alignment: .leading, spacing: 20) {
                    Text("Briefing")
                        .font(.system(size: 24, weight: .bold))
                        .foregroundColor(AppColors.textPrimary)

                    if isLoading {
                        LoadingCard()
                    } else if let watchlist = watchlist {
                        VStack(alignment: .leading, spacing: 12) {
                            Text("Watchlist")
                                .font(.system(size: 18, weight: .semibold))
                                .foregroundColor(AppColors.textPrimary)

                            ForEach(watchlist.items.prefix(5)) { item in
                                HStack {
                                    Text(item.label ?? item.value)
                                        .foregroundColor(AppColors.textSecondary)
                                    Spacer()
                                    Text(item.kind.uppercased())
                                        .font(.system(size: 11, weight: .semibold))
                                        .foregroundColor(AppColors.accent)
                                }
                                .padding(.horizontal, 14)
                                .padding(.vertical, 10)
                                .background(AppColors.card)
                                .cornerRadius(14)
                                .overlay(
                                    RoundedRectangle(cornerRadius: 14)
                                        .stroke(AppColors.cardBorder, lineWidth: 1)
                                )
                            }
                        }

                        VStack(alignment: .leading, spacing: 12) {
                            Text("Laatste signalen")
                                .font(.system(size: 18, weight: .semibold))
                                .foregroundColor(AppColors.textPrimary)
                            ForEach(watchlist.trades.prefix(5)) { trade in
                                TradeCardView(trade: trade)
                            }
                        }
                    }
                }
                .padding(.horizontal, 20)
                .padding(.vertical, 16)
            }
        }
        .navigationBarHidden(true)
        .task {
            await loadWatchlist()
        }
        .refreshable {
            await loadWatchlist()
        }
    }

    private func loadWatchlist() async {
        isLoading = true
        defer { isLoading = false }
        do {
            watchlist = try await APIClient.shared.request("api/watchlist")
        } catch {
            watchlist = nil
        }
    }
}

struct AccountView: View {
    @EnvironmentObject private var session: AppSession
    @State private var settings: SettingsResponse?

    var body: some View {
        ZStack {
            AppScreenBackground()
            ScrollView {
                VStack(alignment: .leading, spacing: 20) {
                    Text("Account")
                        .font(.system(size: 28, weight: .bold))
                        .foregroundColor(AppColors.textPrimary)

                    AppCard {
                        HStack(spacing: 14) {
                            Circle()
                                .fill(AppColors.cardSoft)
                                .frame(width: 56, height: 56)
                                .overlay(
                                    Image(systemName: "person.fill")
                                        .foregroundColor(AppColors.textPrimary)
                                )
                            VStack(alignment: .leading, spacing: 4) {
                                Text(session.user ?? "Account")
                                    .font(.system(size: 18, weight: .semibold))
                                    .foregroundColor(AppColors.textPrimary)
                                Text("Premium member")
                                    .font(.system(size: 12))
                                    .foregroundColor(AppColors.textSecondary)
                            }
                            Spacer()
                        }
                    }

                    SettingsCard()

                    SubscriptionCard()

                    VStack(alignment: .leading, spacing: 12) {
                        Text("Tools")
                            .font(.system(size: 18, weight: .semibold))
                            .foregroundColor(AppColors.textPrimary)

                        NavigationLink(destination: PeopleListView()) {
                            ToolRow(title: "People", icon: "person.3.fill")
                        }
                        NavigationLink(destination: FormsHubView()) {
                            ToolRow(title: "Forms", icon: "doc.text.fill")
                        }
                        NavigationLink(destination: PortfolioView()) {
                            ToolRow(title: "Portfolio", icon: "chart.pie.fill")
                        }
                        NavigationLink(destination: PricesView()) {
                            ToolRow(title: "Prices", icon: "waveform.path.ecg")
                        }
                        NavigationLink(destination: WatchlistView()) {
                            ToolRow(title: "Watchlist", icon: "star.fill")
                        }
                    }

                    Button {
                        Task { await session.logout() }
                    } label: {
                        Text("Logout")
                            .font(.system(size: 14, weight: .semibold))
                            .foregroundColor(AppColors.textPrimary)
                            .frame(maxWidth: .infinity)
                            .padding(.vertical, 12)
                            .background(Color.clear)
                            .overlay(
                                RoundedRectangle(cornerRadius: 14)
                                    .stroke(AppColors.cardBorder, lineWidth: 1)
                            )
                    }
                }
                .padding(.horizontal, 20)
                .padding(.vertical, 16)
            }
        }
        .navigationBarHidden(true)
        .task {
            await loadSettings()
        }
    }

    private func loadSettings() async {
        settings = try? await APIClient.shared.request("api/settings")
    }
}

struct LoginView: View {
    @EnvironmentObject private var session: AppSession
    @State private var isSubmitting = false

    var body: some View {
        ScrollView {
            VStack(spacing: 24) {
                VStack(spacing: 6) {
                    Text("Wolf of Washington")
                        .font(.system(size: 32, weight: .bold))
                        .foregroundColor(AppColors.textPrimary)
                    Text("Volg het geld in de politiek")
                        .font(.system(size: 16))
                        .foregroundColor(AppColors.textSecondary)
                }
                .padding(.top, 40)

                Text("Inloggen")
                    .font(.system(size: 22, weight: .semibold))
                    .foregroundColor(AppColors.textPrimary)

                Text("Log in met je Apple ID om verder te gaan.")
                    .font(.system(size: 14))
                    .foregroundColor(AppColors.textMuted)
                    .multilineTextAlignment(.center)

                if let error = session.lastError {
                    Text(error)
                        .font(.system(size: 12))
                        .foregroundColor(AppColors.danger)
                }

                HStack(spacing: 12) {
                    Rectangle()
                        .fill(AppColors.divider)
                        .frame(height: 1)
                    Text("Of inloggen met")
                        .font(.system(size: 12, weight: .medium))
                        .foregroundColor(AppColors.textMuted)
                    Rectangle()
                        .fill(AppColors.divider)
                        .frame(height: 1)
                }

                ZStack {
                    SignInWithAppleButton(.signIn, onRequest: { request in
                        request.requestedScopes = [.fullName, .email]
                    }, onCompletion: { result in
                        handleAppleResult(result)
                    })
                    .signInWithAppleButtonStyle(.black)
                    .frame(height: 52)
                    .clipShape(RoundedRectangle(cornerRadius: 14))

                    if isSubmitting {
                        ProgressView()
                            .tint(.white)
                    }
                }
                .disabled(isSubmitting)

                HStack(spacing: 6) {
                    Text("Heb je nog geen account?")
                        .foregroundColor(AppColors.textSecondary)
                    Text("Registreren")
                        .foregroundColor(AppColors.accent)
                }
                .font(.system(size: 14, weight: .semibold))
            }
            .padding(.horizontal, 28)
            .padding(.bottom, 40)
        }
    }

    private func handleAppleResult(_ result: Result<ASAuthorization, Error>) {
        switch result {
        case .success(let authorization):
            guard let credential = authorization.credential as? ASAuthorizationAppleIDCredential else {
                session.lastError = "Apple login failed."
                return
            }
            submitAppleCredential(credential)
        case .failure(let error):
            if let authError = error as? ASAuthorizationError, authError.code == .canceled {
                session.lastError = nil
                return
            }
            session.lastError = "Apple login failed."
        }
    }

    private func submitAppleCredential(_ credential: ASAuthorizationAppleIDCredential) {
        guard let tokenData = credential.identityToken,
              let identityToken = String(data: tokenData, encoding: .utf8)
        else {
            session.lastError = "Apple login failed."
            return
        }

        let fullName = credential.fullName.flatMap { components -> String? in
            let formatter = PersonNameComponentsFormatter()
            let formatted = formatter.string(from: components).trimmingCharacters(in: .whitespacesAndNewlines)
            return formatted.isEmpty ? nil : formatted
        }

        session.lastError = nil
        isSubmitting = true
        Task {
            defer { isSubmitting = false }
            _ = await session.loginWithApple(
                identityToken: identityToken,
                email: credential.email,
                fullName: fullName
            )
        }
    }
}

struct TradeCardView: View {
    let trade: Trade

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(alignment: .center) {
                VStack(alignment: .leading, spacing: 4) {
                    Text(trade.displayTicker)
                        .font(.system(size: 20, weight: .semibold))
                        .foregroundColor(AppColors.textPrimary)
                    Text(trade.displayCompany)
                        .font(.system(size: 12))
                        .foregroundColor(AppColors.textMuted)
                }
                Spacer()
                BuySellBadge(label: trade.buySellLabel, isBuy: trade.isBuy)
            }

            AppDivider()

            HStack(spacing: 12) {
                Circle()
                    .fill(AppColors.cardSoft)
                    .frame(width: 36, height: 36)
                    .overlay(
                        Text(String(trade.displayPerson.prefix(1)))
                            .font(.system(size: 16, weight: .bold))
                            .foregroundColor(AppColors.accent)
                    )
                VStack(alignment: .leading, spacing: 2) {
                    Text(trade.displayPerson)
                        .font(.system(size: 14, weight: .semibold))
                        .foregroundColor(AppColors.textPrimary)
                    Text(trade.transactionDate ?? "-")
                        .font(.system(size: 12))
                        .foregroundColor(AppColors.textMuted)
                }
                Spacer()
                VStack(alignment: .trailing, spacing: 4) {
                    Text(trade.displayAmount)
                        .font(.system(size: 14, weight: .semibold))
                        .foregroundColor(AppColors.textPrimary)
                    if let delay = trade.displayDelay {
                        Text(delay)
                            .font(.system(size: 11))
                            .foregroundColor(AppColors.accent)
                    }
                }
            }
        }
        .padding(16)
        .background(AppColors.card)
        .cornerRadius(18)
        .overlay(
            RoundedRectangle(cornerRadius: 18)
                .stroke(AppColors.cardBorder, lineWidth: 1)
        )
    }
}

struct PreviewRow: View {
    let title: String
    let value: String

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(title)
                .font(.system(size: 12))
                .foregroundColor(AppColors.textMuted)
            Text(value)
                .font(.system(size: 15, weight: .semibold))
                .foregroundColor(AppColors.textPrimary)
        }
    }
}

struct AppDivider: View {
    var body: some View {
        Rectangle()
            .fill(AppColors.cardBorder.opacity(0.6))
            .frame(height: 1)
    }
}

struct AppActionButton: View {
    let title: String
    let filled: Bool

    var body: some View {
        Text(title)
            .font(.system(size: 14, weight: .semibold))
            .foregroundColor(filled ? .black : AppColors.textPrimary)
            .padding(.horizontal, 18)
            .padding(.vertical, 12)
            .frame(maxWidth: .infinity)
            .background(filled ? AppColors.accent : Color.clear)
            .overlay(
                RoundedRectangle(cornerRadius: 12)
                    .stroke(AppColors.cardBorder, lineWidth: 1)
            )
            .cornerRadius(12)
    }
}

struct BuySellBadge: View {
    let label: String
    let isBuy: Bool

    var body: some View {
        HStack(spacing: 6) {
            Circle()
                .fill(isBuy ? AppColors.success : AppColors.danger)
                .frame(width: 16, height: 16)
                .overlay(
                    Image(systemName: isBuy ? "arrow.up" : "arrow.down")
                        .font(.system(size: 9, weight: .bold))
                        .foregroundColor(.black)
                )
            Text(label.uppercased())
                .font(.system(size: 12, weight: .bold))
                .foregroundColor(isBuy ? AppColors.success : AppColors.danger)
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 6)
        .background(
            Capsule()
                .fill(AppColors.cardSoft)
        )
    }
}

struct NewsCardView: View {
    let card: NewsCard

    var body: some View {
        HStack(spacing: 14) {
            VStack(alignment: .leading, spacing: 8) {
                Text(card.tag)
                    .font(.system(size: 11, weight: .semibold))
                    .foregroundColor(AppColors.accent)
                    .padding(.horizontal, 10)
                    .padding(.vertical, 4)
                    .background(AppColors.cardSoft)
                    .cornerRadius(8)

                Text(card.title)
                    .font(.system(size: 16, weight: .semibold))
                    .foregroundColor(AppColors.textPrimary)
                Text(card.subtitle)
                    .font(.system(size: 12))
                    .foregroundColor(AppColors.textSecondary)
                Text(card.dateText)
                    .font(.system(size: 11))
                    .foregroundColor(AppColors.textMuted)
            }
            Spacer()
            RoundedRectangle(cornerRadius: 12)
                .fill(
                    LinearGradient(
                        colors: [AppColors.accent.opacity(0.7), AppColors.cardSoft],
                        startPoint: .topLeading,
                        endPoint: .bottomTrailing
                    )
                )
                .frame(width: 90, height: 90)
                .overlay(
                    Image(systemName: "chart.line.uptrend.xyaxis")
                        .font(.system(size: 26, weight: .bold))
                        .foregroundColor(.black)
                )
        }
        .padding(16)
        .background(AppColors.card)
        .cornerRadius(18)
        .overlay(
            RoundedRectangle(cornerRadius: 18)
                .stroke(AppColors.cardBorder, lineWidth: 1)
        )
    }
}

struct SettingsCard: View {
    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("Instellingen")
                .font(.system(size: 18, weight: .semibold))
                .foregroundColor(AppColors.textPrimary)

            SettingsRow(icon: "bell.fill", title: "Push-meldingen", value: nil)
            SettingsRow(icon: "globe", title: "Taal", value: "Nederlands")
        }
        .padding(16)
        .background(AppColors.card)
        .cornerRadius(18)
        .overlay(
            RoundedRectangle(cornerRadius: 18)
                .stroke(AppColors.cardBorder, lineWidth: 1)
        )
    }
}

struct SettingsRow: View {
    let icon: String
    let title: String
    let value: String?

    var body: some View {
        HStack {
            Image(systemName: icon)
                .foregroundColor(AppColors.textSecondary)
                .frame(width: 28)
            Text(title)
                .foregroundColor(AppColors.textPrimary)
            Spacer()
            if let value = value {
                Text(value)
                    .foregroundColor(AppColors.textMuted)
            }
            Image(systemName: "chevron.right")
                .foregroundColor(AppColors.textMuted)
        }
        .font(.system(size: 14))
        .padding(.vertical, 6)
    }
}

struct SubscriptionCard: View {
    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Nu abonneren")
                .font(.system(size: 18, weight: .semibold))
                .foregroundColor(AppColors.textPrimary)
            Text("Krijg toegang tot alle transacties van politici en realtime meldingen")
                .font(.system(size: 13))
                .foregroundColor(AppColors.textSecondary)

            VStack(alignment: .leading, spacing: 10) {
                Text("Maandelijks Pro Abonnement")
                    .font(.system(size: 14, weight: .semibold))
                    .foregroundColor(AppColors.textPrimary)
                HStack(alignment: .firstTextBaseline, spacing: 6) {
                    Text("EUR 49.00")
                        .font(.system(size: 26, weight: .bold))
                        .foregroundColor(AppColors.accent)
                    Text("/month")
                        .font(.system(size: 12))
                        .foregroundColor(AppColors.textMuted)
                }
                FeatureRow(text: "Full access to politician trades")
                FeatureRow(text: "Real-time notifications")
                FeatureRow(text: "Performance analytics")
                FeatureRow(text: "Portfolio insights")

                Button {
                } label: {
                    Text("Subscribe Monthly")
                        .font(.system(size: 14, weight: .semibold))
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, 12)
                        .background(AppColors.accent)
                        .cornerRadius(14)
                        .foregroundColor(.black)
                }
            }
            .padding(14)
            .background(AppColors.cardHighlight)
            .cornerRadius(16)
            .overlay(
                RoundedRectangle(cornerRadius: 16)
                    .stroke(AppColors.cardSoft, lineWidth: 1)
            )
        }
        .padding(16)
        .background(AppColors.card)
        .cornerRadius(18)
        .overlay(
            RoundedRectangle(cornerRadius: 18)
                .stroke(AppColors.cardBorder, lineWidth: 1)
        )
    }
}

struct FeatureRow: View {
    let text: String

    var body: some View {
        HStack(spacing: 8) {
            Image(systemName: "checkmark.circle.fill")
                .foregroundColor(AppColors.accent)
            Text(text)
                .font(.system(size: 12))
                .foregroundColor(AppColors.textSecondary)
        }
    }
}

struct ToolRow: View {
    let title: String
    let icon: String

    var body: some View {
        HStack(spacing: 12) {
            Image(systemName: icon)
                .foregroundColor(AppColors.accent)
                .frame(width: 28)
            Text(title)
                .foregroundColor(AppColors.textPrimary)
            Spacer()
            Image(systemName: "chevron.right")
                .foregroundColor(AppColors.textMuted)
        }
        .padding(12)
        .background(AppColors.card)
        .cornerRadius(14)
        .overlay(
            RoundedRectangle(cornerRadius: 14)
                .stroke(AppColors.cardBorder, lineWidth: 1)
        )
    }
}

struct AppLogoMark: View {
    var body: some View {
        HStack(spacing: 12) {
            ZStack {
                RoundedRectangle(cornerRadius: 16)
                    .fill(AppColors.cardSoft)
                    .frame(width: 52, height: 52)
                Image(systemName: "pawprint.fill")
                    .font(.system(size: 22, weight: .bold))
                    .foregroundColor(AppColors.accent)
            }
            VStack(alignment: .leading, spacing: 2) {
                Text("Wolf of Washington")
                    .font(.system(size: 18, weight: .bold))
                    .foregroundColor(AppColors.textPrimary)
                Text("Insider Trading Intelligence")
                    .font(.system(size: 12))
                    .foregroundColor(AppColors.textMuted)
            }
        }
    }
}

struct SearchBar: View {
    @Binding var text: String
    let placeholder: String
    let onSubmit: () -> Void

    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: "magnifyingglass")
                .foregroundColor(AppColors.textMuted)
            TextField(placeholder, text: $text)
                .textInputAutocapitalization(.never)
                .foregroundColor(AppColors.textPrimary)
                .onSubmit(onSubmit)
        }
        .padding(12)
        .background(AppColors.card)
        .cornerRadius(14)
        .overlay(
            RoundedRectangle(cornerRadius: 14)
                .stroke(AppColors.cardBorder, lineWidth: 1)
        )
    }
}

struct FilterPills: View {
    @Binding var selected: TradeFilter

    var body: some View {
        HStack(spacing: 12) {
            ForEach(TradeFilter.allCases, id: \.self) { filter in
                Button {
                    selected = filter
                } label: {
                    Text(filter.rawValue)
                        .font(.system(size: 13, weight: .semibold))
                        .foregroundColor(selected == filter ? .black : AppColors.textSecondary)
                        .padding(.horizontal, 16)
                        .padding(.vertical, 8)
                        .background(selected == filter ? AppColors.accent : Color.clear)
                        .overlay(
                            RoundedRectangle(cornerRadius: 16)
                                .stroke(
                                    selected == filter ? AppColors.accent : AppColors.cardBorder,
                                    lineWidth: 1
                                )
                        )
                        .cornerRadius(16)
                }
            }
        }
    }
}

struct TagPill: View {
    let text: String
    let color: Color

    var body: some View {
        Text(text)
            .font(.system(size: 11, weight: .bold))
            .foregroundColor(color)
            .padding(.horizontal, 10)
            .padding(.vertical, 4)
            .background(color.opacity(0.2))
            .cornerRadius(10)
    }
}

struct StatCard: View {
    let title: String
    let value: String

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(title)
                .font(.system(size: 11))
                .foregroundColor(AppColors.textMuted)
            Text(value)
                .font(.system(size: 18, weight: .semibold))
                .foregroundColor(AppColors.textPrimary)
        }
        .padding(14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(AppColors.card)
        .cornerRadius(16)
        .overlay(
            RoundedRectangle(cornerRadius: 16)
                .stroke(AppColors.cardBorder, lineWidth: 1)
        )
    }
}

struct LoadingCard: View {
    var body: some View {
        HStack {
            ProgressView()
                .tint(AppColors.accent)
            Text("Laden...")
                .font(.system(size: 13))
                .foregroundColor(AppColors.textSecondary)
        }
        .padding(16)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(AppColors.card)
        .cornerRadius(16)
        .overlay(
            RoundedRectangle(cornerRadius: 16)
                .stroke(AppColors.cardBorder, lineWidth: 1)
        )
    }
}

struct ErrorCard: View {
    let message: String

    var body: some View {
        Text(message)
            .font(.system(size: 13))
            .foregroundColor(AppColors.danger)
            .padding(16)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(AppColors.card)
            .cornerRadius(16)
            .overlay(
                RoundedRectangle(cornerRadius: 16)
                    .stroke(AppColors.cardBorder, lineWidth: 1)
            )
    }
}

struct TradeDetailBridgeView: View {
    let trade: Trade

    var body: some View {
        VStack(spacing: 16) {
            if let ticker = trade.ticker {
                NavigationLink(destination: CompanyDetailView(ticker: ticker)) {
                    ToolRow(title: "Company: \(ticker)", icon: "building.2.fill")
                }
            }
            if let slug = trade.personSlug {
                NavigationLink(destination: PersonDetailView(slug: slug)) {
                    ToolRow(title: "Person: \(trade.displayPerson)", icon: "person.fill")
                }
            }
            Spacer()
        }
        .padding(20)
        .background(AppColors.background)
        .navigationTitle("Detail")
    }
}

struct PeopleListView: View {
    @State private var response: PeopleResponse?
    @State private var query = ""
    @State private var isLoading = false

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                Text("People")
                    .font(.system(size: 24, weight: .bold))
                    .foregroundColor(AppColors.textPrimary)

                SearchBar(text: $query, placeholder: "Zoek politicus") {
                    Task { await loadPeople() }
                }

                if isLoading {
                    LoadingCard()
                } else if let items = response?.items {
                    ForEach(items) { person in
                        NavigationLink(destination: PersonDetailView(slug: person.slug)) {
                            ToolRow(title: person.name, icon: "person.fill")
                        }
                    }
                }
            }
            .padding(.horizontal, 20)
            .padding(.vertical, 16)
        }
        .background(AppColors.background)
        .task { await loadPeople() }
    }

    private func loadPeople() async {
        isLoading = true
        defer { isLoading = false }
        response = try? await APIClient.shared.request(
            "api/people",
            query: ["q": query]
        )
    }
}

struct PersonDetailView: View {
    let slug: String
    @State private var detail: PersonDetailResponse?
    @State private var isLoading = false

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                Text(detail?.name ?? slug)
                    .font(.system(size: 24, weight: .bold))
                    .foregroundColor(AppColors.textPrimary)

                if let summary = detail?.summary {
                    AppCard {
                        Text(summary)
                            .font(.system(size: 13))
                            .foregroundColor(AppColors.textSecondary)
                    }
                }

                if let forms = detail?.forms {
                    AppCard {
                        VStack(alignment: .leading, spacing: 8) {
                            Text("Forms")
                                .font(.system(size: 14, weight: .semibold))
                                .foregroundColor(AppColors.textPrimary)
                            ForEach(forms) { form in
                                HStack {
                                    Text(form.label)
                                        .foregroundColor(AppColors.textSecondary)
                                    Spacer()
                                    Text("\(form.count)")
                                        .foregroundColor(AppColors.textPrimary)
                                }
                                .font(.system(size: 12))
                            }
                        }
                    }
                }

                if let trades = detail?.trades {
                    Text("Recente trades")
                        .font(.system(size: 18, weight: .semibold))
                        .foregroundColor(AppColors.textPrimary)
                    ForEach(trades) { trade in
                        TradeCardView(trade: trade)
                    }
                }
            }
            .padding(.horizontal, 20)
            .padding(.vertical, 16)
        }
        .background(AppColors.background)
        .navigationTitle("Politicus Profiel")
        .task { await loadDetail() }
    }

    private func loadDetail() async {
        isLoading = true
        defer { isLoading = false }
        detail = try? await APIClient.shared.request("api/people/\(slug)")
    }
}

struct CompanyDetailView: View {
    let ticker: String
    @State private var detail: CompanyDetailResponse?

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                Text(detail?.companyName ?? ticker)
                    .font(.system(size: 24, weight: .bold))
                    .foregroundColor(AppColors.textPrimary)

                if let price = detail?.latestPrice {
                    AppCard {
                        HStack {
                            VStack(alignment: .leading, spacing: 6) {
                                Text("Price")
                                    .font(.system(size: 12))
                                    .foregroundColor(AppColors.textMuted)
                                Text(String(format: "$%.2f", price))
                                    .font(.system(size: 18, weight: .semibold))
                                    .foregroundColor(AppColors.textPrimary)
                            }
                            Spacer()
                            if let date = detail?.latestPriceDate {
                                Text(date)
                                    .font(.system(size: 11))
                                    .foregroundColor(AppColors.textMuted)
                            }
                        }
                    }
                }

                if let trades = detail?.trades {
                    Text("Recente trades")
                        .font(.system(size: 18, weight: .semibold))
                        .foregroundColor(AppColors.textPrimary)
                    ForEach(trades) { trade in
                        TradeCardView(trade: trade)
                    }
                }
            }
            .padding(.horizontal, 20)
            .padding(.vertical, 16)
        }
        .background(AppColors.background)
        .navigationTitle(ticker)
        .task {
            detail = try? await APIClient.shared.request("api/companies/\(ticker)")
        }
    }
}

struct FormsHubView: View {
    private let forms: [(label: String, api: String)] = [
        ("Form 4 (Insiders)", "FORM 4"),
        ("Congress", "CONGRESS"),
        ("Form 3", "FORM 3"),
        ("Schedule 13D", "SCHEDULE 13D"),
        ("Form 13F", "FORM 13F"),
        ("Form 8-K", "FORM 8-K"),
        ("Form 10-K", "FORM 10-K")
    ]

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 12) {
                Text("Forms")
                    .font(.system(size: 24, weight: .bold))
                    .foregroundColor(AppColors.textPrimary)

                ForEach(forms, id: \.api) { form in
                    NavigationLink(destination: FormTradesView(form: form.api, title: form.label)) {
                        ToolRow(title: form.label, icon: "doc.text.fill")
                    }
                }
            }
            .padding(.horizontal, 20)
            .padding(.vertical, 16)
        }
        .background(AppColors.background)
    }
}

struct FormTradesView: View {
    let form: String
    let title: String
    @State private var trades: [Trade] = []

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 12) {
                Text(title)
                    .font(.system(size: 22, weight: .bold))
                    .foregroundColor(AppColors.textPrimary)

                ForEach(trades) { trade in
                    TradeCardView(trade: trade)
                }
            }
            .padding(.horizontal, 20)
            .padding(.vertical, 16)
        }
        .background(AppColors.background)
        .task {
            let response: TradesResponse? = try? await APIClient.shared.request(
                "api/trades",
                query: ["form": form, "limit": "50"]
            )
            trades = response?.items ?? []
        }
    }
}

struct WatchlistView: View {
    @State private var watchlist: WatchlistResponse?

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                Text("Watchlist")
                    .font(.system(size: 24, weight: .bold))
                    .foregroundColor(AppColors.textPrimary)

                if let items = watchlist?.items {
                    ForEach(items) { item in
                        HStack {
                            Text(item.label ?? item.value)
                                .foregroundColor(AppColors.textPrimary)
                            Spacer()
                            Text(item.kind.uppercased())
                                .font(.system(size: 11, weight: .bold))
                                .foregroundColor(AppColors.accent)
                        }
                        .padding(12)
                        .background(AppColors.card)
                        .cornerRadius(12)
                    }
                }
            }
            .padding(.horizontal, 20)
            .padding(.vertical, 16)
        }
        .background(AppColors.background)
        .task {
            watchlist = try? await APIClient.shared.request("api/watchlist")
        }
    }
}

struct PortfolioView: View {
    @State private var portfolio: PortfolioResponse?

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                Text("Portfolio")
                    .font(.system(size: 24, weight: .bold))
                    .foregroundColor(AppColors.textPrimary)

                if let transactions = portfolio?.transactions {
                    Text("Laatste transacties")
                        .font(.system(size: 16, weight: .semibold))
                        .foregroundColor(AppColors.textPrimary)
                    ForEach(transactions.prefix(8)) { tx in
                        AppCard {
                            HStack {
                                VStack(alignment: .leading, spacing: 4) {
                                    Text(tx.symbol ?? "-")
                                        .font(.system(size: 14, weight: .semibold))
                                        .foregroundColor(AppColors.textPrimary)
                                    Text(tx.brokerLabel ?? "Broker")
                                        .font(.system(size: 12))
                                        .foregroundColor(AppColors.textMuted)
                                }
                                Spacer()
                                Text(tx.tradeDate ?? "-")
                                    .font(.system(size: 11))
                                    .foregroundColor(AppColors.textMuted)
                            }
                        }
                    }
                }

                if let imports = portfolio?.imports, !imports.isEmpty {
                    Text("Imports")
                        .font(.system(size: 16, weight: .semibold))
                        .foregroundColor(AppColors.textPrimary)
                    ForEach(imports) { item in
                        AppCard {
                            VStack(alignment: .leading, spacing: 4) {
                                Text(item.source.uppercased())
                                    .font(.system(size: 12, weight: .bold))
                                    .foregroundColor(AppColors.accent)
                                Text(item.message ?? "Import")
                                    .font(.system(size: 12))
                                    .foregroundColor(AppColors.textSecondary)
                            }
                        }
                    }
                }
            }
            .padding(.horizontal, 20)
            .padding(.vertical, 16)
        }
        .background(AppColors.background)
        .task {
            portfolio = try? await APIClient.shared.request("api/portfolio")
        }
    }
}

struct PricesView: View {
    @State private var ticker = ""
    @State private var data: PricesResponse?

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                Text("Prices")
                    .font(.system(size: 24, weight: .bold))
                    .foregroundColor(AppColors.textPrimary)

                SearchBar(text: $ticker, placeholder: "Ticker (AAPL)") {
                    Task { await loadPrices() }
                }

                if let data = data, !data.values.isEmpty {
                    LineChart(values: data.values)
                        .frame(height: 180)
                        .padding(.top, 8)
                } else {
                    Text("Geen data")
                        .font(.system(size: 12))
                        .foregroundColor(AppColors.textMuted)
                }
            }
            .padding(.horizontal, 20)
            .padding(.vertical, 16)
        }
        .background(AppColors.background)
    }

    private func loadPrices() async {
        guard !ticker.isEmpty else { return }
        data = try? await APIClient.shared.request(
            "api/prices",
            query: ["ticker": ticker.uppercased(), "range": "1m"]
        )
    }
}

struct LineChart: View {
    let values: [Double]

    var body: some View {
        GeometryReader { geo in
            let maxValue = values.max() ?? 1
            let minValue = values.min() ?? 0
            let range = max(maxValue - minValue, 1)

            Path { path in
                for index in values.indices {
                    let x = geo.size.width * CGFloat(index) / CGFloat(max(values.count - 1, 1))
                    let yPosition = (values[index] - minValue) / range
                    let y = geo.size.height * (1 - CGFloat(yPosition))
                    if index == 0 {
                        path.move(to: CGPoint(x: x, y: y))
                    } else {
                        path.addLine(to: CGPoint(x: x, y: y))
                    }
                }
            }
            .stroke(AppColors.accent, style: StrokeStyle(lineWidth: 2, lineCap: .round))
        }
        .background(AppColors.card)
        .cornerRadius(16)
    }
}

struct AppCard<Content: View>: View {
    let content: Content

    init(@ViewBuilder content: () -> Content) {
        self.content = content()
    }

    var body: some View {
        content
            .padding(14)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(AppColors.card)
            .cornerRadius(16)
            .overlay(
                RoundedRectangle(cornerRadius: 16)
                    .stroke(AppColors.cardBorder, lineWidth: 1)
            )
    }
}
