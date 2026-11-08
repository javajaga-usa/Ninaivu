/*
 * ninaivu: the portable folder's one starter, as Ninaivu.exe is on Windows
 * and Ninaivu.app on a Mac. Double-clicked, it opens Ninaivu (the Control
 * Panel, its tray icon, and the server started); in a terminal it takes the
 * same options, ./ninaivu --server, --where, --bring and the rest.
 *
 * A Linux file manager opens a shell script in a text editor when it is
 * double-clicked (GNOME Files, and Thunar on MX Linux unless told otherwise),
 * but runs a program. So this is a program, and all it does is find the
 * folder it is in and run app/ninaivu.sh there with the same arguments: the
 * script sets the folders and opens Ninaivu. Built static by
 * installers/linux/build.sh, so it runs on any Linux of its architecture,
 * whatever C library the computer has.
 *
 * It is called "ninaivu", in small letters, the name the script at the top
 * of the folder had until 4.0: a Secondary on 3.x that is sent this
 * download by its Primary looks for that name and starts "ninaivu --server",
 * and a service set up with `ninaivu mugil setup` runs it by that path.
 */
#include <errno.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

int main(int argc, char **argv)
{
    char self[PATH_MAX];
    ssize_t length = readlink("/proc/self/exe", self, sizeof self - 1);
    if (length <= 0) {
        perror("Ninaivu: cannot tell which folder this is in");
        return 1;
    }
    self[length] = '\0';
    char *slash = strrchr(self, '/');
    if (slash == NULL) {
        return 1;
    }
    *slash = '\0';

    char script[PATH_MAX];
    if (snprintf(script, sizeof script, "%s/app/ninaivu.sh", self) >= (int)sizeof script) {
        fprintf(stderr, "Ninaivu: the folder's path is too long\n");
        return 1;
    }
    if (chdir(self) != 0) {
        perror("Ninaivu: cannot open its folder");
        return 1;
    }

    char **args = calloc((size_t)argc + 2, sizeof *args);
    if (args == NULL) {
        return 1;
    }
    args[0] = "sh";
    args[1] = script;
    for (int i = 1; i < argc; i++) {
        args[i + 1] = argv[i];
    }
    execv("/bin/sh", args);
    fprintf(stderr, "Ninaivu: cannot run %s: %s\n", script, strerror(errno));
    return 1;
}
