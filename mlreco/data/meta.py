"""Module with a data class object which represents rasterized images metadata.

This copies the internal structure of either :class:`larcv.ImageMeta` for 2D
images or :class:`larcv.Voxel3DMeta` for 3D images.
"""

from dataclasses import dataclass

import numpy as np

from .base import DataStructBase

__all__ = ['Meta']


@dataclass
class Meta(DataStructBase):
    """Meta information about a rasterized image.

    Attributes
    ----------
    lower : np.ndarray
        (2/3) Array of image lower bounds in detector coordinates (cm)
    upper : np.ndarray
        (2/3) Array of image upper bounds in detector coordinates (cm)
    size : np.ndarray
        (2/3) Array of pixel/voxel size in each dimension (cm)
    """
    lower: np.ndarray = None
    upper: np.ndarray = None
    size: np.ndarray = None

    # Fixed-length attributes
    _fixed_length_attrs = {'lower': 3, 'upper': 3, 'size': 3}

    def to_cm(self, coords, translate=True):
        """Converts pixel indexes in a tensor to detector coordinates in cm.

        Parameters
        ----------
        coords : np.ndarray
            (N, 2/3) Input pixel indices
        translate : bool, default True
            If set to `False`, this function returns the input unchanged
        """
        if not translate or len(coords) == 0:
            return coords

        out = self.lower + (coords + .5) * self.size
        return out.astype(np.float32)

    def to_pixel(self, coords, translate=True):
        """Converts detector coordinates in cm in a tensor to pixel indexes.

        Parameters
        ----------
        coords : np.ndarray
            (N, 2/3) Input detector coordinates
        translate : bool, default True
            If set to `False`, this function returns the input unchanged
        """
        if not translate or len(coords) == 0:
            return coords

        return (coords - self.lower) / self.size - .5

    @classmethod
    def from_larcv(cls, meta):
        """Builds and returns a Meta object from a LArCV 2D metadata object.

        Parameters
        ----------
        meta : Union[larcv.ImageMeta, larcv.Voxel3DMeta]
            LArCV-format 2D metadata

        Returns
        -------
        Meta
            Metadata object
        """
        if hasattr(meta, 'pos_z'):
            lower = np.array([meta.min_x(), meta.min_y(), meta.min_z()])
            upper = np.array([meta.max_x(), meta.max_y(), meta.max_z()])
            size  = np.array([meta.size_voxel_x(),
                              meta.size_voxel_y(),
                              meta.size_voxel_z()])
        else:
            lower = np.array([meta.min_x(), meta.min_y()])
            upper = np.array([meta.max_x(), meta.max_y()])
            size  = np.array([meta.pixel_width(), meta.pixel_height()])

        return cls(lower=lower, upper=upper, size=size)