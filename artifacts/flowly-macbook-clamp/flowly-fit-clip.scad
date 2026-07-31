// Flowly Fit Clip — editable OpenSCAD source (millimetres)
// Slim vertical face, short lower-case jaw and open diagonal truss.

$fn = 80;

module rounded_box(size, radius, center = true) {
    translate(center ? -size / 2 : [0, 0, 0])
        hull()
            for (x = [radius, size[0] - radius])
                for (y = [radius, size[1] - radius])
                    for (z = [radius, size[2] - radius])
                        translate([x, y, z]) sphere(r = radius);
}

module capsule_x(length, radius) {
    hull() {
        translate([-length / 2, 0, 0]) sphere(r = radius);
        translate([ length / 2, 0, 0]) sphere(r = radius);
    }
}

module logo_end() {
    translate([0, -1.5, 9]) rounded_box([10, 5, 18], 2.2);
}

module lower_jaw() {
    union() {
        translate([0, 12.5, 0.5]) rounded_box([8.5, 25, 1], 0.42);
        translate([0, 9.5, 1.13]) capsule_x(6, 0.18);
    }
}

module upper_jaw() {
    union() {
        // Compliant central tongue: 4.0 to 5.8 mm nominal opening.
        hull() {
            translate([0, 1.5, 5.92]) rounded_box([6, 3, 1.6], 0.62);
            translate([0, 21.5, 7.49]) rounded_box([6, 3, 1.6], 0.62);
        }
        translate([0, 9.5, 5.72]) capsule_x(4.8, 0.20);
    }
}

module diagonal_rail(x_position) {
    hull() {
        translate([x_position, 1.5, 16.40]) rounded_box([1.2, 3, 1.7], 0.48);
        translate([x_position, 21.5, 9.10]) rounded_box([1.2, 3, 1.7], 0.48);
    }
}

module screen_bead() {
    translate([0, -0.2, 17.85]) capsule_x(5.6, 0.50);
}

module logo_cut() {
    translate([0, -3.45, 9])
        rotate([90, 0, 0])
            linear_extrude(height = 0.75)
                resize([6.8, 6.8], auto = false)
                    import("flowly-logo.svg", center = true);
}

difference() {
    union() {
        logo_end();
        lower_jaw();
        upper_jaw();
        diagonal_rail(-3.1);
        diagonal_rail(3.1);
        screen_bead();
    }
    logo_cut();
}
