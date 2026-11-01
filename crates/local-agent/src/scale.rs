//! Screenshot downscaling without image crates.

/// Box-average downscale of an RGBA buffer so the longest side fits `max_side`.
pub fn downscale(width: u32, height: u32, rgba: &[u8], max_side: u32) -> (u32, u32, Vec<u8>) {
    let longest = width.max(height);
    if longest <= max_side || width == 0 || height == 0 {
        return (width, height, rgba.to_vec());
    }
    let w = ((width as u64 * max_side as u64) / longest as u64).max(1) as u32;
    let h = ((height as u64 * max_side as u64) / longest as u64).max(1) as u32;
    let mut out = vec![0u8; (w * h * 4) as usize];
    for oy in 0..h {
        let y0 = (oy as u64 * height as u64 / h as u64) as u32;
        let y1 = (((oy + 1) as u64 * height as u64 / h as u64) as u32).max(y0 + 1);
        for ox in 0..w {
            let x0 = (ox as u64 * width as u64 / w as u64) as u32;
            let x1 = (((ox + 1) as u64 * width as u64 / w as u64) as u32).max(x0 + 1);
            let mut sum = [0u64; 4];
            for y in y0..y1 {
                let row = (y * width) as usize * 4;
                for x in x0..x1 {
                    let i = row + x as usize * 4;
                    for c in 0..4 {
                        sum[c] += rgba[i + c] as u64;
                    }
                }
            }
            let n = ((y1 - y0) * (x1 - x0)) as u64;
            let o = ((oy * w + ox) * 4) as usize;
            for c in 0..4 {
                out[o + c] = (sum[c] / n) as u8;
            }
        }
    }
    (w, h, out)
}

#[cfg(test)]
mod tests {
    use super::downscale;

    #[test]
    fn downscale_fits_longest_side_and_averages() {
        let (w, h, px) = downscale(4, 2, &[255u8; 4 * 2 * 4], 2);
        assert_eq!((w, h), (2, 1));
        assert_eq!(px, vec![255u8; 2 * 4]);
        let mut checker = Vec::new();
        for i in 0..4 {
            let v = if i % 2 == 0 { 0 } else { 200 };
            checker.extend_from_slice(&[v, v, v, 255]);
        }
        let (_, _, px) = downscale(4, 1, &checker, 2);
        assert_eq!(&px[..4], &[100, 100, 100, 255]);
        let (w, h, _) = downscale(800, 600, &vec![0u8; 800 * 600 * 4], 1280);
        assert_eq!((w, h), (800, 600));
    }
}
