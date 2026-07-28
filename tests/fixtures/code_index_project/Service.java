package demo;

public class Service {
    private final String name;

    public Service(String name) {
        this.name = name;
    }

    public String greet() {
        return "hello " + name;
    }
}

interface Worker {
    void run();
}
