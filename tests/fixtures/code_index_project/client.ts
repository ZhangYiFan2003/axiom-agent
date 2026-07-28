interface ApiClient {
    fetchUser(id: string): Promise<string>;
}

type UserId = string;

class HttpClient implements ApiClient {
    async fetchUser(id: string): Promise<string> {
        return id;
    }
}

const normalizeUser = (id: UserId): string => {
    return id.trim();
};

function buildClient(): ApiClient {
    return new HttpClient();
}
