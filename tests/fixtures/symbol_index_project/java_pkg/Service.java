package java_pkg;

import java.util.List;
import java_pkg.Helper;

public class Service {
    private Helper helper;

    public Service() {
        this.helper = new Helper();
    }

    public boolean run(String value) {
        Helper.staticName();
        return helper.validate(value);
    }
}
